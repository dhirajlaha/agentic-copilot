"""Reads one file from a repository at a given commit (GitHub contents API, raw media type)."""

import logging
from urllib.parse import quote

import httpx

from app.core.exceptions import GitHubError, RepositoryAccessError
from app.indexing import policy
from app.integrations.github_repos import API_URL, REPO_NAME

logger = logging.getLogger(__name__)

RAW_MEDIA_TYPE = "application/vnd.github.raw+json"


class GitHubFileReader:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def read_text(self, repo_full_name: str, ref: str, path: str, token: str) -> str | None:
        """The file's text at `ref`, or None if there is no such file."""
        if not REPO_NAME.match(repo_full_name):
            raise RepositoryAccessError(f"Invalid repository name: {repo_full_name!r}")
        try:
            response = self._http.get(
                f"{API_URL}/repos/{repo_full_name}/contents/{quote(path, safe='/')}",
                params={"ref": ref},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": RAW_MEDIA_TYPE,
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"Could not reach GitHub to read {path}") from exc
        if response.status_code == 404:
            return None
        if response.status_code == 403 and "too_large" in response.text:
            raise RepositoryAccessError(f"{path} is too large to read")
        if (
            response.status_code in (403, 429)
            and response.headers.get("x-ratelimit-remaining") == "0"
        ):
            raise RepositoryAccessError("GitHub API rate limit reached; try again later")
        if response.status_code >= 400:
            logger.warning("GitHub returned %s reading a file", response.status_code)
            raise GitHubError(f"GitHub returned {response.status_code} reading {path}")
        if response.headers.get("content-type", "").split(";")[0].strip() == "application/json":
            # A directory (or submodule) comes back as JSON rather than file content; raw
            # file content is served as application/vnd.github.raw.
            raise RepositoryAccessError(f"{path} is not a regular file")
        data = response.content
        if len(data) > policy.MAX_FILE_BYTES:
            raise RepositoryAccessError(f"{path} is too large to read")
        text = policy.decode_text(data)
        if text is None:
            raise RepositoryAccessError(f"{path} is not a text file")
        return text
