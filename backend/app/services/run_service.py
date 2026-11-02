"""API-side run use cases: queue a run for a ticket and read a run's progress.

Execution happens in the worker (`app/services/run_executor.py`), never in a request.
"""

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.db.enums import RunStatus, TicketStatus
from app.db.models import AgentRun, User
from app.repositories.run_repository import AgentRunRepository
from app.repositories.ticket_repository import TicketRepository

ONE_ACTIVE_RUN_INDEX = "uq_agent_runs_one_active_per_ticket"


class RunService:
    def __init__(
        self, session: Session, tickets: TicketRepository, runs: AgentRunRepository
    ) -> None:
        self._session = session
        self._tickets = tickets
        self._runs = runs

    def start(self, user: User, ticket_id: UUID) -> AgentRun:
        ticket = self._tickets.get_owned(ticket_id, user.id)
        if ticket is None:
            raise NotFoundError("Ticket")
        try:
            run = self._runs.add(AgentRun(ticket_id=ticket.id, status=RunStatus.PENDING))
        except IntegrityError as exc:
            # The partial unique index rejected a second active run. Checking in the
            # database (not with a prior SELECT) is what makes this race-free.
            self._session.rollback()
            if ONE_ACTIVE_RUN_INDEX in str(exc.orig):
                raise ConflictError("A run is already in progress for this ticket") from exc
            raise
        ticket.status = TicketStatus.RUNNING
        self._session.commit()
        return run

    def get(self, user: User, run_id: UUID) -> AgentRun:
        run = self._runs.get_owned_with_steps(run_id, user.id)
        if run is None:
            raise NotFoundError("Run")
        return run
