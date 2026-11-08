"""Reads a repository's text files as of one commit, via GitHub's tarball endpoint.

One download instead of one API request per file: a repository with thousands of files
would otherwise burn the installation's hourly rate limit. The tarball is streamed (never
held whole in memory), filtered by `app.indexing.policy`, and bounded in size.
"""

import io
import logging
import tarfile
from collections.abc import Iterator
from pathlib import PurePosixPath

import httpx

from app.core.exceptions import GitHubError, RepositoryAccessError
from app.indexing import policy
from app.indexing.snapshot import Snapshot, SnapshotFile, git_blob_sha
from app.integrations.github_repos import API_URL, REPO_NAME

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # compressed
MAX_TEXT_BYTES = 150 * 1024 * 1024  # decoded text kept for indexing
COMMIT_SHA_MEDIA_TYPE = "application/vnd.github.sha"


class _BoundedStream(io.RawIOBase):
    """Presents an iterator of byte chunks as a file, failing if it exceeds `limit`."""

    def __init__(self, chunks: Iterator[bytes], limit: int) -> None:
        self._chunks = chunks
        self._limit = limit
        self._read = 0
        self._buffer = b""

    def readable(self) -> bool:
        return True

    def readinto(self, target: bytearray) -> int:  # type: ignore[override]
        while not self._buffer:
            try:
                self._buffer = next(self._chunks)
            except StopIteration:
                return 0
        size = min(len(target), len(self._buffer))
        target[:size] = self._buffer[:size]
        self._buffer = self._buffer[size:]
        self._read += size
        if self._read > self._limit:
            raise RepositoryAccessError("The repository is too large to index")
        return size


class GitHubSnapshotReader:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def head_commit(self, repo_full_name: str, token: str) -> str:
        self._check_name(repo_full_name)
        repo = self._get_json(f"/repos/{repo_full_name}", token, repo_full_name)
        branch = repo.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise RepositoryAccessError(f"{repo_full_name} has no default branch (empty repo?)")
        response = self._get(
            f"/repos/{repo_full_name}/commits/{branch}",
            token,
            repo_full_name,
            accept=COMMIT_SHA_MEDIA_TYPE,
        )
        sha = response.text.strip()
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise GitHubError("Unexpected commit response from GitHub")
        return sha

    def snapshot(self, repo_full_name: str, token: str, commit_sha: str) -> Snapshot:
        self._check_name(repo_full_name)
        if len(commit_sha) != 40:
            raise ValueError("commit_sha must be a full 40-character SHA")
        snapshot = Snapshot(commit_sha=commit_sha)
        try:
            with self._open_tarball(repo_full_name, token, commit_sha) as response:
                stream = io.BufferedReader(
                    _BoundedStream(response.iter_bytes(), MAX_DOWNLOAD_BYTES)
                )
                with tarfile.open(fileobj=stream, mode="r|gz") as archive:
                    self._read_archive(archive, snapshot)
        except (httpx.HTTPError, tarfile.TarError, EOFError, OSError) as exc:
            raise GitHubError(f"Could not download {repo_full_name}") from exc
        return snapshot

    # -- internals ---------------------------------------------------------------------

    def _read_archive(self, archive: tarfile.TarFile, snapshot: Snapshot) -> None:
        kept_bytes = 0
        for member in archive:
            if not member.isfile():  # skips directories, symlinks and the pax header
                continue
            path = self._relative_path(member.name)
            if path is None:
                continue
            reason = policy.skip_reason_for_path(path) or policy.skip_reason_for_size(member.size)
            if reason is None:
                extracted = archive.extractfile(member)
                data = extracted.read() if extracted else b""
                text = policy.decode_text(data)
                if text is None:
                    reason = policy.SkipReason.BINARY
                elif not text.strip():
                    reason = policy.SkipReason.EMPTY
                elif policy.contains_secret(text):
                    reason = policy.SkipReason.POSSIBLE_SECRET
            if reason is not None:
                snapshot.skipped[reason.value] += 1
                if reason in (policy.SkipReason.SECRET_FILE, policy.SkipReason.POSSIBLE_SECRET):
                    snapshot.secret_paths.append(path)
                continue
            kept_bytes += len(data)
            if kept_bytes > MAX_TEXT_BYTES:
                raise RepositoryAccessError("The repository is too large to index")
            snapshot.files[path] = SnapshotFile(path=path, sha=git_blob_sha(data), text=text)

    @staticmethod
    def _relative_path(name: str) -> str | None:
        """Drop the archive's top-level folder (`owner-repo-<sha>/`) and reject anything
        that could point outside the repository."""
        parts = PurePosixPath(name).parts[1:]
        if not parts or any(part in ("", ".", "..") for part in parts):
            return None
        return "/".join(parts)

    def _open_tarball(self, repo: str, token: str, commit_sha: str):
        redirect = self._get(
            f"/repos/{repo}/tarball/{commit_sha}", token, repo, allow_redirect=True
        )
        location = redirect.headers.get("location")
        if redirect.status_code not in (301, 302, 307) or not location:
            raise GitHubError("Unexpected tarball response from GitHub")
        if not location.startswith("https://"):
            raise GitHubError("Refusing a non-HTTPS tarball location")
        # The redirect target is a pre-signed URL on another host: do NOT send the token.
        return self._http.stream("GET", location)

    def _get_json(self, path: str, token: str, repo: str) -> dict:
        try:
            body = self._get(path, token, repo).json()
        except ValueError as exc:
            raise GitHubError("Unexpected response from GitHub") from exc
        if not isinstance(body, dict):
            raise GitHubError("Unexpected response from GitHub")
        return body

    def _get(
        self,
        path: str,
        token: str,
        repo: str,
        *,
        accept: str = "application/vnd.github+json",
        allow_redirect: bool = False,
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = self._http.get(f"{API_URL}{path}", headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubError(f"Could not reach GitHub for {repo}") from exc
        if allow_redirect and response.status_code in (301, 302, 307):
            return response
        if response.status_code == 404:
            raise RepositoryAccessError(
                f"{repo} was not found or the GitHub App can no longer access it"
            )
        if (
            response.status_code in (403, 429)
            and response.headers.get("x-ratelimit-remaining") == "0"
        ):
            raise RepositoryAccessError("GitHub API rate limit reached; try again later")
        if response.status_code >= 400:
            logger.warning("GitHub %s returned %s for %s", path, response.status_code, repo)
            raise GitHubError(f"GitHub returned {response.status_code} for {repo}")
        return response

    @staticmethod
    def _check_name(repo_full_name: str) -> None:
        if not REPO_NAME.match(repo_full_name):
            raise RepositoryAccessError(f"Invalid repository name: {repo_full_name!r}")
