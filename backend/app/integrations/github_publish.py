"""Writes a patch to GitHub: one commit on a new branch, then a draft pull request.

Uses the Git Data API (blobs/trees/commits/refs) rather than the per-file contents API, so a
patch touching several files becomes ONE commit, not one per file. The new branch points at a
commit whose parent is the exact base commit the patch was checked against; the default branch
is never touched, and nothing is force-pushed or merged.
"""

import logging
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.core.exceptions import GitHubError, RepositoryAccessError
from app.integrations.github_repos import API_URL, REPO_NAME

logger = logging.getLogger(__name__)

DEFAULT_MODE = "100644"  # a regular, non-executable file
FORBIDDEN_HINT = (
    "GitHub refused to let the app write to this repository. Check that the app has "
    "Contents and Pull requests write access and that the installation accepted it."
)


@dataclass(frozen=True, slots=True)
class FileWrite:
    path: str
    content: str | None  # None deletes the file


@dataclass(frozen=True, slots=True)
class PublishedPullRequest:
    branch: str
    number: int
    url: str


class PullRequestPublisher(Protocol):
    def publish(
        self,
        *,
        repo_full_name: str,
        token: str,
        base_commit_sha: str,
        branch: str,
        files: list[FileWrite],
        commit_message: str,
        pr_title: str,
        pr_body: str,
    ) -> PublishedPullRequest: ...


class GitHubPullRequestPublisher:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def publish(
        self,
        *,
        repo_full_name: str,
        token: str,
        base_commit_sha: str,
        branch: str,
        files: list[FileWrite],
        commit_message: str,
        pr_title: str,
        pr_body: str,
    ) -> PublishedPullRequest:
        if not REPO_NAME.match(repo_full_name):
            raise RepositoryAccessError(f"Invalid repository name: {repo_full_name!r}")
        repo = repo_full_name
        default_branch = self._call("GET", f"/repos/{repo}", token)["default_branch"]
        base_tree = self._call("GET", f"/repos/{repo}/git/commits/{base_commit_sha}", token)[
            "tree"
        ]["sha"]

        entries: list[dict[str, Any]] = []
        for file in files:
            mode = self._mode_of(repo, base_tree, file.path, token) or DEFAULT_MODE
            entry: dict[str, Any] = {"path": file.path, "mode": mode, "type": "blob"}
            if file.content is None:
                entry["sha"] = None  # a null sha removes the path from the tree
            else:
                entry["content"] = file.content
            entries.append(entry)

        tree = self._call(
            "POST", f"/repos/{repo}/git/trees", token, {"base_tree": base_tree, "tree": entries}
        )["sha"]
        commit = self._call(
            "POST",
            f"/repos/{repo}/git/commits",
            token,
            {"message": commit_message, "tree": tree, "parents": [base_commit_sha]},
        )["sha"]

        ref_response = self._call(
            "POST",
            f"/repos/{repo}/git/refs",
            token,
            {"ref": f"refs/heads/{branch}", "sha": commit},
            accept_unprocessable=True,
        )
        if ref_response.get("_unprocessable"):
            # The branch exists from an earlier attempt that died before recording its pull
            # request. Reuse that pull request if there is one; never overwrite a branch.
            existing = self._find_pull_request(repo, branch, token)
            if existing is not None:
                return existing
            raise GitHubError(
                f"Branch {branch} already exists without a pull request; delete it on GitHub "
                "and publish again"
            )

        try:
            pull = self._call(
                "POST",
                f"/repos/{repo}/pulls",
                token,
                {
                    "title": pr_title,
                    "head": branch,
                    "base": default_branch,
                    "body": pr_body,
                    "draft": True,
                },
            )
        except Exception:
            self._delete_branch(repo, branch, token)  # do not leave an orphan branch behind
            raise
        return PublishedPullRequest(branch=branch, number=int(pull["number"]), url=pull["html_url"])

    # -- internals ---------------------------------------------------------------------

    def _mode_of(self, repo: str, tree_sha: str, path: str, token: str) -> str | None:
        """The file mode (e.g. 100755 for an executable) of an existing path, so editing a
        script does not silently drop its executable bit. None if the path does not exist."""
        parts = path.split("/")
        current = tree_sha
        for index, part in enumerate(parts):
            listing = self._call("GET", f"/repos/{repo}/git/trees/{current}", token)
            if listing.get("truncated"):
                raise GitHubError(f"A directory in {path} is too large to inspect")
            entry = next((e for e in listing.get("tree", []) if e.get("path") == part), None)
            if entry is None:
                return None
            if index == len(parts) - 1:
                return entry["mode"] if entry.get("type") == "blob" else None
            if entry.get("type") != "tree":
                return None
            current = entry["sha"]
        return None

    def _find_pull_request(self, repo: str, branch: str, token: str) -> PublishedPullRequest | None:
        owner = repo.split("/")[0]
        pulls = self._call(
            "GET",
            f"/repos/{repo}/pulls?head={quote(owner)}:{quote(branch, safe='')}&state=all",
            token,
        )
        if isinstance(pulls, list) and pulls:
            return PublishedPullRequest(branch, int(pulls[0]["number"]), pulls[0]["html_url"])
        return None

    def _delete_branch(self, repo: str, branch: str, token: str) -> None:
        try:
            self._http.delete(
                f"{API_URL}/repos/{repo}/git/refs/heads/{quote(branch, safe='/')}",
                headers=self._headers(token),
            )
        except httpx.HTTPError:
            logger.warning("could not clean up branch after a failed pull request")

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _call(
        self,
        method: str,
        path: str,
        token: str,
        body: dict[str, Any] | None = None,
        *,
        accept_unprocessable: bool = False,
    ) -> Any:
        try:
            response = self._http.request(
                method, f"{API_URL}{path}", headers=self._headers(token), json=body
            )
        except httpx.HTTPError as exc:
            raise GitHubError("Could not reach GitHub") from exc
        if response.status_code == 404:
            raise RepositoryAccessError(
                "GitHub could not find the repository or commit, or the app can no longer access it"
            )
        if (
            response.status_code in (403, 429)
            and response.headers.get("x-ratelimit-remaining") == "0"
        ):
            raise RepositoryAccessError("GitHub API rate limit reached; try again later")
        if response.status_code in (401, 403):
            logger.warning("GitHub refused %s %s (%s)", method, path, response.status_code)
            raise RepositoryAccessError(FORBIDDEN_HINT)
        if response.status_code == 422 and accept_unprocessable:
            return {"_unprocessable": True}
        if response.status_code >= 400:
            logger.warning("GitHub %s %s returned %s", method, path, response.status_code)
            raise GitHubError(f"GitHub returned {response.status_code} for {method} {path}")
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubError("Unexpected response from GitHub") from exc
