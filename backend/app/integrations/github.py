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


@dataclass(frozen=True, slots=True)
class GitHubUser:
    id: int
    login: str
    email: str | None
    avatar_url: str | None


class GitHubOAuth(Protocol):
    """What the auth service needs from GitHub. Tests provide a fake implementation."""

    def authorization_url(self, state: str, code_challenge: str) -> str: ...

    def fetch_user(self, code: str, code_verifier: str) -> GitHubUser: ...


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
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
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
