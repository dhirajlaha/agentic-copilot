import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.exceptions import AuthenticationError, OAuthStateError
from app.core.security import (
    SignedCookieCodec,
    generate_pkce_pair,
    generate_token,
    hash_token,
    safe_next_path,
)
from app.db.models import User
from app.integrations.github import GitHubOAuth
from app.repositories.session_repository import UserSessionRepository
from app.repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LoginStart:
    authorization_url: str
    state_cookie: str  # signed value to put in the short-lived state cookie


@dataclass(frozen=True, slots=True)
class LoginResult:
    session_token: str  # raw token for the session cookie; only its hash is stored
    expires_at: datetime
    next_path: str


class AuthService:
    """Sign-in with GitHub, session issuing and validation."""

    def __init__(
        self,
        *,
        session: Session,
        users: UserRepository,
        sessions: UserSessionRepository,
        github: GitHubOAuth,
        state_codec: SignedCookieCodec,
        session_ttl_seconds: int,
        oauth_state_ttl_seconds: int,
    ) -> None:
        self._session = session
        self._users = users
        self._sessions = sessions
        self._github = github
        self._state_codec = state_codec
        self._session_ttl = timedelta(seconds=session_ttl_seconds)
        self._state_ttl_seconds = oauth_state_ttl_seconds

    def start_login(self, next_path: str | None) -> LoginStart:
        state = generate_token()
        verifier, challenge = generate_pkce_pair()
        state_cookie = self._state_codec.dumps(
            {"state": state, "verifier": verifier, "next": safe_next_path(next_path)}
        )
        return LoginStart(self._github.authorization_url(state, challenge), state_cookie)

    def complete_login(
        self,
        *,
        code: str,
        state: str,
        state_cookie: str | None,
        now: datetime | None = None,
    ) -> LoginResult:
        """Verify the OAuth state, fetch the GitHub user, upsert them and open a session."""
        if not state_cookie:
            raise OAuthStateError("Missing state cookie")
        payload = self._state_codec.loads(state_cookie, self._state_ttl_seconds)
        expected = payload.get("state")
        if not isinstance(expected, str) or not secrets.compare_digest(expected, state):
            raise OAuthStateError("State mismatch")

        verifier = payload.get("verifier")
        if not isinstance(verifier, str) or not verifier:
            raise OAuthStateError("Missing PKCE verifier")

        github_user = self._github.fetch_user(code, verifier)
        user = self._users.upsert_from_github(
            github_id=github_user.id,
            login=github_user.login,
            email=github_user.email,
            avatar_url=github_user.avatar_url,
        )
        now = now or datetime.now(UTC)
        token = generate_token()
        expires_at = now + self._session_ttl
        self._sessions.add(user_id=user.id, token_hash=hash_token(token), expires_at=expires_at)
        self._session.commit()
        logger.info("user signed in", extra={"user_id": str(user.id)})
        return LoginResult(token, expires_at, safe_next_path(payload.get("next")))

    def authenticate(self, session_token: str | None, now: datetime | None = None) -> User:
        if not session_token:
            raise AuthenticationError("Not signed in")
        user = self._sessions.get_user_for_token(
            hash_token(session_token), now or datetime.now(UTC)
        )
        if user is None:
            raise AuthenticationError("Session is invalid or expired")
        return user

    def logout(self, session_token: str | None) -> None:
        if session_token:
            self._sessions.delete_by_token_hash(hash_token(session_token))
            self._session.commit()
