from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.enums import IndexStatus
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

    # -- code index state ---------------------------------------------------------------

    def claim_index(self, repo_connection_id: UUID, now: datetime, lease_until: datetime) -> bool:
        """Take the right to index this repository, unless another worker holds a live
        lease on it. One atomic UPDATE, so two workers cannot both win."""
        claimed = self._session.execute(
            update(RepoConnection)
            .where(
                RepoConnection.id == repo_connection_id,
                or_(
                    RepoConnection.index_status != IndexStatus.INDEXING,
                    RepoConnection.index_lease_expires_at.is_(None),
                    RepoConnection.index_lease_expires_at < now,
                ),
            )
            .values(
                index_status=IndexStatus.INDEXING,
                index_lease_expires_at=lease_until,
                index_error=None,
            )
            .returning(RepoConnection.id)
        ).first()
        return claimed is not None

    def renew_index_lease(self, repo_connection_id: UUID, lease_until: datetime) -> None:
        self._session.execute(
            update(RepoConnection)
            .where(RepoConnection.id == repo_connection_id)
            .values(index_lease_expires_at=lease_until)
        )

    def finish_index(
        self,
        repo_connection_id: UUID,
        *,
        status: IndexStatus,
        now: datetime,
        commit_sha: str | None = None,
        error: str | None = None,
    ) -> None:
        values: dict[str, object] = {
            "index_status": status,
            "index_lease_expires_at": None,
            "index_error": error,
        }
        if status == IndexStatus.READY:
            values |= {"indexed_commit_sha": commit_sha, "indexed_at": now}
        self._session.execute(
            update(RepoConnection).where(RepoConnection.id == repo_connection_id).values(**values)
        )

    def get(self, repo_connection_id: UUID) -> RepoConnection | None:
        return self._session.get(RepoConnection, repo_connection_id, populate_existing=True)
