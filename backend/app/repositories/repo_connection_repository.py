from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
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

    def list_for_user(self, user_id: UUID) -> list[RepoConnection]:
        return list(
            self._session.scalars(
                select(RepoConnection)
                .where(RepoConnection.user_id == user_id)
                .order_by(RepoConnection.repo_full_name)
            )
        )

    def add(
        self, *, user_id: UUID, github_installation_id: UUID, repo_full_name: str
    ) -> RepoConnection | None:
        """Connect a repository. Returns None if this user already connected it; the
        unique (user, repository) constraint decides, so concurrent requests are safe."""
        stmt = (
            insert(RepoConnection)
            .values(
                user_id=user_id,
                github_installation_id=github_installation_id,
                repo_full_name=repo_full_name,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "repo_full_name"])
            .returning(RepoConnection)
        )
        return self._session.scalars(stmt, execution_options={"populate_existing": True}).first()

    def delete(self, repo: RepoConnection) -> None:
        # ORM delete so the tickets / chunks cascade configured on the model applies.
        self._session.delete(repo)
        self._session.flush()
