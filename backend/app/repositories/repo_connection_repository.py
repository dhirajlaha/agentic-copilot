from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RepoConnection


class RepoConnectionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_owned(self, repo_connection_id: UUID, user_id: UUID) -> RepoConnection | None:
        return self._session.scalar(
            select(RepoConnection).where(
                RepoConnection.id == repo_connection_id,
                RepoConnection.user_id == user_id,
            )
        )
