from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.db.enums import RunStatus
from app.db.models import AgentRun, AgentStep, Ticket


class AgentRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, run: AgentRun) -> AgentRun:
        self._session.add(run)
        self._session.flush()
        return run

    def get(self, run_id: UUID) -> AgentRun | None:
        return self._session.get(AgentRun, run_id)

    def get_owned_with_steps(self, run_id: UUID, user_id: UUID) -> AgentRun | None:
        """A run with its steps, only if the run's ticket belongs to `user_id`."""
        return self._session.scalar(
            select(AgentRun)
            .join(Ticket, Ticket.id == AgentRun.ticket_id)
            .where(AgentRun.id == run_id, Ticket.user_id == user_id)
            .options(selectinload(AgentRun.steps))
        )

    def claim_next(self, now: datetime, lease_until: datetime) -> AgentRun | None:
        """Atomically take the oldest pending run and mark it running.

        `FOR UPDATE SKIP LOCKED` makes concurrent workers skip rows another worker is
        claiming instead of waiting on them, so each run is claimed exactly once.
        """
        next_pending = (
            select(AgentRun.id)
            .where(AgentRun.status == RunStatus.PENDING)
            .order_by(AgentRun.created_at, AgentRun.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        stmt = (
            update(AgentRun)
            .where(AgentRun.id == next_pending)
            .values(status=RunStatus.RUNNING, started_at=now, lease_expires_at=lease_until)
            .returning(AgentRun)
        )
        return self._session.scalars(
            stmt, execution_options={"populate_existing": True}
        ).one_or_none()

    def extend_lease(self, run_id: UUID, lease_until: datetime) -> None:
        self._session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == RunStatus.RUNNING)
            .values(lease_expires_at=lease_until)
        )

    def finish(self, run_id: UUID, status: RunStatus, now: datetime, error: str | None) -> None:
        self._session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(status=status, finished_at=now, lease_expires_at=None, error=error)
        )

    def fail_expired(self, now: datetime, error: str) -> list[UUID]:
        """Fail running runs whose worker stopped renewing the lease. Returns ticket ids."""
        result = self._session.execute(
            update(AgentRun)
            .where(AgentRun.status == RunStatus.RUNNING, AgentRun.lease_expires_at < now)
            .values(status=RunStatus.FAILED, finished_at=now, lease_expires_at=None, error=error)
            .returning(AgentRun.ticket_id)
        )
        return list(result.scalars())


class AgentStepRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, run_id: UUID, record: dict[str, Any]) -> AgentStep:
        step = AgentStep(
            run_id=run_id,
            agent_name=record["agent_name"],
            input=record["input"],
            output=record["output"],
            tokens_used=record["tokens_used"],
            latency_ms=record["latency_ms"],
        )
        self._session.add(step)
        self._session.flush()
        return step
