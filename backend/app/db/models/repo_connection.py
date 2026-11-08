import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAt, UuidPk, status_check
from app.db.enums import IndexStatus
from app.db.models.github_installation import GitHubInstallation
from app.db.models.user import User

if TYPE_CHECKING:
    from app.db.models.code_chunk import CodeChunk
    from app.db.models.ticket import Ticket


class RepoConnection(Base):
    __tablename__ = "repo_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "repo_full_name"),
        status_check("index_status", IndexStatus, "ck_repo_connections_index_status"),
    )

    id: Mapped[UuidPk]
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    repo_full_name: Mapped[str] = mapped_column(String(255))  # "owner/repo"
    # RESTRICT: removing an installation link must not silently delete connected repos
    # (and through them tickets and runs); disconnect the repos first.
    github_installation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("github_installations.id", ondelete="RESTRICT"), index=True
    )
    connected_at: Mapped[CreatedAt]

    # Code index state. A run keeps the index in sync with the default branch before the
    # agents read it; `index_lease_expires_at` stops two workers indexing one repo at once.
    index_status: Mapped[str] = mapped_column(
        String(32), server_default=text(f"'{IndexStatus.NOT_INDEXED.value}'")
    )
    index_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indexed_commit_sha: Mapped[str | None] = mapped_column(String(40))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    index_error: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="repo_connections")
    github_installation: Mapped[GitHubInstallation] = relationship(
        back_populates="repo_connections"
    )
    # Deleting a repo deletes its tickets and indexed chunks (DELETE /repos/{id}).
    tickets: Mapped[list["Ticket"]] = relationship(
        back_populates="repo_connection", cascade="all, delete-orphan"
    )
    code_chunks: Mapped[list["CodeChunk"]] = relationship(
        back_populates="repo_connection", cascade="all, delete-orphan"
    )
