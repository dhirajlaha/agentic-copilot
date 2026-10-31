import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAt, UuidPk, status_check
from app.db.enums import TicketStatus
from app.db.models.repo_connection import RepoConnection
from app.db.models.user import User

if TYPE_CHECKING:
    from app.db.models.agent import AgentRun


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (status_check("status", TicketStatus, "ck_tickets_status"),)

    id: Mapped[UuidPk]
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    repo_connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repo_connections.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(
        String(32), default=TicketStatus.OPEN, server_default=TicketStatus.OPEN.value
    )
    created_at: Mapped[CreatedAt]

    user: Mapped[User] = relationship(back_populates="tickets")
    repo_connection: Mapped[RepoConnection] = relationship(back_populates="tickets")
    runs: Mapped[list["AgentRun"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan"
    )
