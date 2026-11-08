"""GitHub OAuth + REST client. The only module that talks to GitHub over HTTP."""

import logging
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from app.core.exceptions import GitHubError, OAuthDeniedError

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105  (a URL, not a secret)
API_URL = "https://api.github.com"
PAGE_SIZE = 100
MAX_PAGES = 10  # a hard stop so a misbehaving response can never loop forever


@dataclass(frozen=True, slots=True)
class GitHubUser:
    id: int
    login: str
    email: str | None
    avatar_url: str | None


@dataclass(frozen=True, slots=True)
class InstallationInfo:
    id: int
    account_login: str
    account_type: str  # "User" or "Organization"


@dataclass(frozen=True, slots=True)
class InstallationGrant:
    """Who authorised, and which App installations that person can access."""

    github_user_id: int
    installations: list[InstallationInfo]


class GitHubOAuth(Protocol):
    """What the auth service needs from GitHub. Tests provide a fake implementation."""

    def authorization_url(self, state: str, code_challenge: str) -> str: ...

    def fetch_user(self, code: str, code_verifier: str) -> GitHubUser: ...

    def fetch_installations(self, code: str, code_verifier: str) -> InstallationGrant: ...


class GitHubOAuthClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        http: httpx.Client,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._http = http

    def authorization_url(self, state: str, code_challenge: str) -> str:
        # No `scope`: this is a GitHub App, whose permissions are fixed on the app itself
        # (GitHub ignores scopes for GitHub Apps).
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    def fetch_user(self, code: str, code_verifier: str) -> GitHubUser:
        """Exchange the authorization code and return the user's profile.

        The access token is used for read-only calls and then discarded; it is never
        stored or logged.
        """
        token = self._exchange_code(code, code_verifier)
        headers = self._api_headers(token)
        profile = self._get_json(f"{API_URL}/user", headers)
        try:
            user_id, login = int(profile["id"]), str(profile["login"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError("Unexpected profile response from GitHub") from exc
        email = profile.get("email")
        if not email:
            # The email is a convenience, not our identity key (that is the GitHub user
            # id), so failing to read it must never block sign-in.
            try:
                email = self._primary_verified_email(headers)
            except GitHubError as exc:
                logger.warning("Could not read the user's email, continuing without: %s", exc)
                email = None
        return GitHubUser(
            id=user_id, login=login, email=email, avatar_url=profile.get("avatar_url")
        )

    def fetch_installations(self, code: str, code_verifier: str) -> InstallationGrant:
        """For the install flow: exchange the code (with PKCE) and list the installations
        the user can access. Used to prove that an `installation_id` from a redirect really
        belongs to the person who came back."""
        token = self._exchange_code(code, code_verifier)
        headers = self._api_headers(token)
        profile = self._get_json(f"{API_URL}/user", headers)
        try:
            user_id = int(profile["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError("Unexpected profile response from GitHub") from exc

        installations: list[InstallationInfo] = []
        for page in range(1, MAX_PAGES + 1):
            body = self._get_json(
                f"{API_URL}/user/installations?per_page={PAGE_SIZE}&page={page}", headers
            )
            try:
                items = body["installations"]
                installations += [
                    InstallationInfo(
                        id=int(item["id"]),
                        account_login=str(item["account"]["login"]),
                        account_type=str(item["account"].get("type", "User")),
                    )
                    for item in items
                ]
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise GitHubError("Unexpected installations response from GitHub") from exc
            if len(items) < PAGE_SIZE:
                break
        return InstallationGrant(github_user_id=user_id, installations=installations)

    @staticmethod
    def _api_headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _exchange_code(self, code: str, code_verifier: str) -> str:
        try:
            response = self._http.post(
                TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                    "redirect_uri": self._redirect_uri,
                    "code_verifier": code_verifier,
                },
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            body: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GitHubError("Token exchange failed") from exc
        # GitHub reports a bad/expired code as HTTP 200 with an "error" field.
        if "error" in body:
            if body["error"] == "access_denied":
                raise OAuthDeniedError()
            logger.warning("GitHub token exchange rejected: %s", body["error"])
            raise GitHubError(f"Token exchange rejected: {body['error']}")
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise GitHubError("No access token in GitHub response")
        return token

    def _primary_verified_email(self, headers: dict[str, str]) -> str | None:
        emails = self._get_json(f"{API_URL}/user/emails", headers)
        if not isinstance(emails, list):
            return None
        for entry in emails:
            if entry.get("primary") and entry.get("verified"):
                return str(entry["email"])
        return None

    def _get_json(self, url: str, headers: dict[str, str]) -> Any:
        try:
            response = self._http.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GitHubError(f"GitHub request failed: {url}") from exc
