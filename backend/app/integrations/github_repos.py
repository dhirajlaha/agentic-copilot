"""Lists a repository's files through the GitHub REST API (git trees).

Public repositories work without credentials (GitHub allows 60 unauthenticated requests
per hour per IP). Private repositories need an installation token, which the repo-connect
flow (GitHub App installation) will pass in as `token`; the code path is the same.
"""

import logging
import re
from typing import Protocol
from urllib.parse import quote

import httpx

from app.core.exceptions import RepositoryAccessError

logger = logging.getLogger(__name__)

API_URL = "https://api.github.com"
# owner/repo as GitHub allows them. Validating before building URLs prevents path
# injection into the API request.
REPO_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")


class RepoFilesProvider(Protocol):
    def list_files(self, repo_full_name: str, token: str | None = None) -> list[str]: ...


class GitHubRepoFiles:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def list_files(self, repo_full_name: str, token: str | None = None) -> list[str]:
        if not REPO_NAME.match(repo_full_name):
            raise RepositoryAccessError(f"Invalid repository name: {repo_full_name!r}")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        repo = self._get(f"/repos/{repo_full_name}", headers, repo_full_name)
        branch = repo.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise RepositoryAccessError(f"{repo_full_name} has no default branch (empty repo?)")
        tree = self._get(
            f"/repos/{repo_full_name}/git/trees/{quote(branch, safe='')}",
            headers,
            repo_full_name,
            params={"recursive": "1"},
        )
        if tree.get("truncated"):
            # GitHub caps recursive trees (~100k entries). A partial list would silently
            # mislead the Planner, so fail loudly instead.
            raise RepositoryAccessError(f"{repo_full_name} is too large to list in one request")
        return [e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"]

    def _get(
        self, path: str, headers: dict[str, str], repo: str, params: dict[str, str] | None = None
    ) -> dict:
        try:
            response = self._http.get(f"{API_URL}{path}", headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise RepositoryAccessError(f"Could not reach GitHub for {repo}") from exc
        if response.status_code == 404:
            raise RepositoryAccessError(
                f"{repo} was not found or is private (private repos need the GitHub App)"
            )
        if (
            response.status_code in (403, 429)
            and response.headers.get("x-ratelimit-remaining") == "0"
        ):
            raise RepositoryAccessError("GitHub API rate limit reached; try again later")
        if response.status_code >= 400:
            logger.warning("GitHub %s returned %s for %s", path, response.status_code, repo)
            raise RepositoryAccessError(f"GitHub returned {response.status_code} for {repo}")
        try:
            body = response.json()
        except ValueError as exc:
            raise RepositoryAccessError("Unexpected response from GitHub") from exc
        if not isinstance(body, dict):
            raise RepositoryAccessError("Unexpected response from GitHub")
        return body
