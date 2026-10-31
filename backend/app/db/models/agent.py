import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAt, UuidPk, status_check
from app.db.enums import RunStatus, TestStatus
from app.db.models.ticket import Ticket


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (status_check("status", RunStatus, "ck_agent_runs_status"),)

    id: Mapped[UuidPk]
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default=RunStatus.PENDING, server_default=RunStatus.PENDING.value
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ticket: Mapped[Ticket] = relationship(back_populates="runs")
    steps: Mapped[list["AgentStep"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="AgentStep.created_at",
    )
    test_results: Mapped[list["TestResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class AgentStep(Base):
    __tablename__ = "agent_steps"

    id: Mapped[UuidPk]
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(64))
    # JSONB: each agent has a different input/output shape (plan, diff, tests, ...).
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tokens_used: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[CreatedAt]

    run: Mapped[AgentRun] = relationship(back_populates="steps")


class TestResult(Base):
    __tablename__ = "test_results"
    __test__ = False  # not a pytest test class
    __table_args__ = (status_check("status", TestStatus, "ck_test_results_status"),)

    id: Mapped[UuidPk]
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    test_name: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32))
    output: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[CreatedAt]

    run: Mapped[AgentRun] = relationship(back_populates="test_results")
