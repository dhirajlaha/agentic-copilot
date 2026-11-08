"""Acting as the GitHub App: sign a short-lived JWT with the app's private key, trade it
for an installation access token, and use that to read what the installation can see."""

import logging
import time
from dataclasses import dataclass
from typing import Protocol

import httpx
import jwt

from app.core.exceptions import GitHubError, RepositoryAccessError
from app.integrations.github import API_URL, MAX_PAGES, PAGE_SIZE

logger = logging.getLogger(__name__)

# GitHub rejects an app JWT valid for more than 10 minutes, and recommends backdating
# `iat` by 60 seconds to tolerate clock drift between servers.
JWT_LIFETIME_SECONDS = 9 * 60
JWT_BACKDATE_SECONDS = 60
GONE = (
    "GitHub no longer lets this app access the repository (the app was uninstalled or "
    "the repository was removed from it). Reconnect it."
)


@dataclass(frozen=True, slots=True)
class InstallationRepo:
    full_name: str
    private: bool


class InstallationTokenSource(Protocol):
    def installation_token(self, installation_id: int) -> str: ...


class GitHubApp(Protocol):
    """What the services need from the GitHub App. Tests provide a fake."""

    def install_url(self) -> str: ...

    def installation_token(self, installation_id: int) -> str: ...

    def list_repositories(self, installation_id: int) -> list[InstallationRepo]: ...


class GitHubAppClient:
    def __init__(
        self, *, client_id: str, private_key_pem: str, slug: str, http: httpx.Client
    ) -> None:
        self._client_id = client_id
        self._private_key = private_key_pem
        self._slug = slug
        self._http = http

    def install_url(self) -> str:
        """Where to send the user to install the app. Its redirect comes back through the
        App's Setup URL, not through our state, so no state is attached."""
        return f"https://github.com/apps/{self._slug}/installations/new"

    def _app_jwt(self) -> str:
        now = int(time.time())
        claims = {
            "iss": self._client_id,
            "iat": now - JWT_BACKDATE_SECONDS,
            "exp": now + JWT_LIFETIME_SECONDS,
        }
        return jwt.encode(claims, self._private_key, algorithm="RS256")

    def installation_token(self, installation_id: int) -> str:
        """A fresh token (valid about an hour) scoped to what the app was granted. It is
        never stored: callers use it immediately and drop it."""
        try:
            response = self._http.post(
                f"{API_URL}/app/installations/{int(installation_id)}/access_tokens",
                headers={
                    "Authorization": f"Bearer {self._app_jwt()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        except httpx.HTTPError as exc:
            raise GitHubError("Could not reach GitHub to get an installation token") from exc
        if response.status_code == 404:
            raise RepositoryAccessError(GONE)
        if response.status_code != 201:
            logger.error("installation token request returned %s", response.status_code)
            raise GitHubError(f"Installation token request failed ({response.status_code})")
        try:
            token = response.json()["token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise GitHubError("Unexpected installation token response") from exc
        if not isinstance(token, str) or not token:
            raise GitHubError("Unexpected installation token response")
        return token

    def list_repositories(self, installation_id: int) -> list[InstallationRepo]:
        headers = {
            "Authorization": f"Bearer {self.installation_token(installation_id)}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        repos: list[InstallationRepo] = []
        for page in range(1, MAX_PAGES + 1):
            try:
                response = self._http.get(
                    f"{API_URL}/installation/repositories",
                    headers=headers,
                    params={"per_page": PAGE_SIZE, "page": page},
                )
                response.raise_for_status()
                items = response.json()["repositories"]
                repos += [
                    InstallationRepo(full_name=str(i["full_name"]), private=bool(i["private"]))
                    for i in items
                ]
            except httpx.HTTPError as exc:
                raise GitHubError("Could not list the installation's repositories") from exc
            except (ValueError, KeyError, TypeError) as exc:
                raise GitHubError("Unexpected repositories response from GitHub") from exc
            if len(items) < PAGE_SIZE:
                break
        return repos
