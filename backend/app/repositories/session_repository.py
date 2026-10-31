from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import User, UserSession


class UserSessionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, *, user_id: UUID, token_hash: str, expires_at: datetime) -> UserSession:
        user_session = UserSession(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
        self._session.add(user_session)
        self._session.flush()
        return user_session

    def get_user_for_token(self, token_hash: str, now: datetime) -> User | None:
        """The user owning a non-expired session with this token hash, if any."""
        return self._session.scalar(
            select(User)
            .join(UserSession, UserSession.user_id == User.id)
            .where(UserSession.token_hash == token_hash, UserSession.expires_at > now)
        )

    def delete_by_token_hash(self, token_hash: str) -> None:
        self._session.execute(delete(UserSession).where(UserSession.token_hash == token_hash))

    def delete_expired(self, now: datetime) -> int:
        result = self._session.execute(delete(UserSession).where(UserSession.expires_at <= now))
        return result.rowcount or 0
