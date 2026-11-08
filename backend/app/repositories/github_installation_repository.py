from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import GitHubInstallation


class GitHubInstallationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self, *, user_id: UUID, installation_id: int, account_login: str, account_type: str
    ) -> GitHubInstallation:
        """Record that this user can access this installation, refreshing the account
        name if it changed. One atomic statement, so a double submit cannot duplicate."""
        stmt = insert(GitHubInstallation).values(
            user_id=user_id,
            installation_id=installation_id,
            account_login=account_login,
            account_type=account_type,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[GitHubInstallation.user_id, GitHubInstallation.installation_id],
            set_={
                "account_login": stmt.excluded.account_login,
                "account_type": stmt.excluded.account_type,
            },
        ).returning(GitHubInstallation)
        return self._session.scalars(stmt, execution_options={"populate_existing": True}).one()

    def get_owned(self, installation_pk: UUID, user_id: UUID) -> GitHubInstallation | None:
        return self._session.scalar(
            select(GitHubInstallation).where(
                GitHubInstallation.id == installation_pk,
                GitHubInstallation.user_id == user_id,
            )
        )

    def list_for_user(self, user_id: UUID) -> list[GitHubInstallation]:
        return list(
            self._session.scalars(
                select(GitHubInstallation)
                .where(GitHubInstallation.user_id == user_id)
                .order_by(GitHubInstallation.account_login)
            )
        )
