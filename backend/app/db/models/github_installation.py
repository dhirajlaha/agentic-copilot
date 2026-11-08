import uuid
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAt, UuidPk
from app.db.models.user import User

if TYPE_CHECKING:
    from app.db.models.repo_connection import RepoConnection


class GitHubInstallation(Base):
    """A GitHub App installation a user has proven they can access.

    Only the installation id is stored. Short-lived (1 hour) installation tokens are
    minted on demand from the app's private key, so there is no long-lived credential in
    the database to leak or rotate.
    """

    __tablename__ = "github_installations"
    # Several users can legitimately reach one installation (members of an organisation),
    # but each proves access separately, so the link is per (user, installation).
    __table_args__ = (UniqueConstraint("user_id", "installation_id"),)

    id: Mapped[UuidPk]
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    installation_id: Mapped[int] = mapped_column(BigInteger)
    account_login: Mapped[str] = mapped_column(String(255))
    account_type: Mapped[str] = mapped_column(String(32))  # "User" or "Organization"
    created_at: Mapped[CreatedAt]

    user: Mapped[User] = relationship()
    repo_connections: Mapped[list["RepoConnection"]] = relationship(
        back_populates="github_installation"
    )
