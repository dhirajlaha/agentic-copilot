from datetime import UTC, datetime, timedelta
from itertools import count

from sqlalchemy.orm import Session

from app.core.security import generate_token, hash_token
from app.db.models import RepoConnection, User, UserSession

SESSION_COOKIE = "__Host-session"  # default session_cookie_name
_ids = count(1000)


def create_user(session: Session, login: str | None = None) -> User:
    n = next(_ids)
    user = User(github_id=n, github_login=login or f"user{n}", email=f"user{n}@example.com")
    session.add(user)
    session.flush()
    return user


def create_repo(session: Session, user: User, name: str | None = None) -> RepoConnection:
    repo = RepoConnection(user_id=user.id, repo_full_name=name or f"octo/repo{next(_ids)}")
    session.add(repo)
    session.flush()
    return repo


def create_session_token(
    session: Session, user: User, expires_in: timedelta = timedelta(hours=1)
) -> str:
    """Insert a login session directly and return the raw cookie token."""
    token = generate_token()
    session.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=datetime.now(UTC) + expires_in,
        )
    )
    session.flush()
    return token
