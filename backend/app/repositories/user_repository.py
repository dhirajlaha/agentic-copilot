from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import User


class UserRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_from_github(
        self, *, github_id: int, login: str, email: str | None, avatar_url: str | None
    ) -> User:
        """Create the user or refresh their GitHub profile fields, atomically.

        A single INSERT ... ON CONFLICT is race-free: two simultaneous first logins for
        the same GitHub account cannot both insert and cannot hit a unique-key error.
        """
        stmt = insert(User).values(
            github_id=github_id, github_login=login, email=email, avatar_url=avatar_url
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[User.github_id],
            set_={
                "github_login": stmt.excluded.github_login,
                "email": stmt.excluded.email,
                "avatar_url": stmt.excluded.avatar_url,
            },
        ).returning(User)
        return self._session.scalars(stmt, execution_options={"populate_existing": True}).one()

    def get_by_github_id(self, github_id: int) -> User | None:
        return self._session.scalar(select(User).where(User.github_id == github_id))
