import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAt, UuidPk
from app.db.models.user import User

if TYPE_CHECKING:
    from app.db.models.code_chunk import CodeChunk
    from app.db.models.ticket import Ticket


class RepoConnection(Base):
    __tablename__ = "repo_connections"
    __table_args__ = (UniqueConstraint("user_id", "repo_full_name"),)

    id: Mapped[UuidPk]
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    repo_full_name: Mapped[str] = mapped_column(String(255))  # "owner/repo"
    # Ciphertext only. Encryption/decryption happens in app code (added with the
    # repo-connect flow); the DB never sees the plaintext token.
    installation_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    connected_at: Mapped[CreatedAt]

    user: Mapped[User] = relationship(back_populates="repo_connections")
    # Deleting a repo deletes its tickets and indexed chunks (DELETE /repos/{id}).
    tickets: Mapped[list["Ticket"]] = relationship(
        back_populates="repo_connection", cascade="all, delete-orphan"
    )
    code_chunks: Mapped[list["CodeChunk"]] = relationship(
        back_populates="repo_connection", cascade="all, delete-orphan"
    )
