import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAt, UuidPk

if TYPE_CHECKING:
    from app.db.models.repo_connection import RepoConnection
    from app.db.models.ticket import Ticket


class User(Base):
    __tablename__ = "users"

    id: Mapped[UuidPk]
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    github_login: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[CreatedAt]

    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    repo_connections: Mapped[list["RepoConnection"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    tickets: Mapped[list["Ticket"]] = relationship(back_populates="user")


class UserSession(Base):
    """A login session. Only the SHA-256 hash of the cookie token is stored, so a
    database leak does not let anyone impersonate a user."""

    __tablename__ = "user_sessions"

    id: Mapped[UuidPk]
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[CreatedAt]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    user: Mapped[User] = relationship(back_populates="sessions")
