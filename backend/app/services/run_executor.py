"""Worker-side execution of queued runs.

Each run: claim it (status running + lease) -> load ticket and repo -> bring the code
index up to date -> list repo files -> stream the agent graph, persisting every step as it
completes and renewing the lease -> mark the run completed or failed and update the ticket.
"""

import dataclasses
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.orm import Session

from app.agents.context import CONTEXT_KEY, RunContext
from app.agents.planner import PlanningError
from app.agents.state import RunInput
from app.agents.steps import StepRecord
from app.core.exceptions import AppError
from app.db.enums import RunStatus, TicketStatus
from app.integrations.github_app import InstallationTokenSource
from app.integrations.github_repos import RepoFilesProvider
from app.llm.client import LLMError
from app.repositories.run_repository import AgentRunRepository, AgentStepRepository
from app.repositories.ticket_repository import TicketRepository
from app.services.index_service import RepoIndexer
from app.services.run_context import RunContextBuilder

logger = logging.getLogger(__name__)

# Errors whose message we wrote and is safe to show the user. Anything else is reported
# generically; details go to the server log only.
USER_SAFE_ERRORS = (AppError, LLMError, PlanningError)
LEASE_EXPIRED_ERROR = "Interrupted: the worker stopped before the run finished. Start it again."


def _now() -> datetime:
    return datetime.now(UTC)


class RunExecutor:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        graph: CompiledStateGraph,
        repo_files: RepoFilesProvider,
        tokens: InstallationTokenSource,
        indexer: RepoIndexer,
        contexts: RunContextBuilder,
        lease_seconds: int,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._session_factory = session_factory
        self._graph = graph
        self._repo_files = repo_files
        self._tokens = tokens
        self._indexer = indexer
        self._contexts = contexts
        self._lease = timedelta(seconds=lease_seconds)
        self._clock = clock

    def reap_expired(self) -> int:
        """Fail runs whose worker died (lease passed) and reopen their tickets."""
        with self._session_factory() as session:
            ticket_ids = AgentRunRepository(session).fail_expired(
                self._clock(), LEASE_EXPIRED_ERROR
            )
            TicketRepository(session).set_status(ticket_ids, TicketStatus.OPEN)
            session.commit()
        if ticket_ids:
            logger.warning("reaped %d run(s) with expired leases", len(ticket_ids))
        return len(ticket_ids)

    def run_next(self) -> UUID | None:
        """Claim and execute the oldest pending run. Returns its id, or None if idle."""
        with self._session_factory() as session:
            now = self._clock()
            run = AgentRunRepository(session).claim_next(now, now + self._lease)
            # Read the id before commit: objects must not be used after their session ends.
            run_id = run.id if run is not None else None
            session.commit()  # the claim is visible to other workers from here
        if run_id is None:
            return None
        logger.info("run started", extra={"run_id": str(run_id)})
        self._execute(run_id)
        return run_id

    def _execute(self, run_id: UUID) -> None:
        with self._session_factory() as session:
            runs, steps = AgentRunRepository(session), AgentStepRepository(session)
            try:
                run_input, context = self._prepare(session, run_id, runs, steps)
                # stream_mode="updates" yields each node's output as soon as it finishes,
                # so the dashboard sees steps live instead of only at the end.
                config = {"configurable": {CONTEXT_KEY: context}}
                for update in self._graph.stream(run_input, config, stream_mode="updates"):
                    for node_output in update.values():
                        for record in (node_output or {}).get("steps", []):
                            steps.add(run_id, record)
                    runs.extend_lease(run_id, self._clock() + self._lease)
                    session.commit()
                self._finish(session, run_id, RunStatus.COMPLETED, TicketStatus.COMPLETED, None)
                logger.info("run completed", extra={"run_id": str(run_id)})
            except USER_SAFE_ERRORS as exc:
                session.rollback()
                logger.warning("run failed: %s", exc, extra={"run_id": str(run_id)})
                self._finish(session, run_id, RunStatus.FAILED, TicketStatus.OPEN, str(exc))
            except Exception:
                session.rollback()
                logger.exception("run crashed", extra={"run_id": str(run_id)})
                self._finish(
                    session,
                    run_id,
                    RunStatus.FAILED,
                    TicketStatus.OPEN,
                    "Internal error while running the pipeline.",
                )

    def _prepare(
        self,
        session: Session,
        run_id: UUID,
        runs: AgentRunRepository,
        steps: AgentStepRepository,
    ) -> tuple[RunInput, RunContext]:
        """Load the ticket, bring the code index up to date (recorded as a step), and
        build the graph's input and the per-run context (files and search)."""
        run = runs.get(run_id)
        if run is None:
            raise AppError("Run disappeared before it could start")
        ticket = TicketRepository(session).get(run.ticket_id)
        if ticket is None:
            raise AppError("Ticket was deleted")
        repo = ticket.repo_connection
        repo_id, repo_name = repo.id, repo.repo_full_name
        # A fresh, short-lived token per run, used only for this run and never stored.
        token = self._tokens.installation_token(repo.github_installation.installation_id)

        started = time.perf_counter()
        result = self._indexer.sync(
            repo_connection_id=repo_id,
            repo_full_name=repo_name,
            token=token,
            heartbeat=lambda: self._renew_lease(run_id),
        )
        steps.add(
            run_id,
            StepRecord(
                agent_name="indexer",
                input={"repo_connection_id": str(repo_id)},
                output=dataclasses.asdict(result),
                tokens_used=None,
                latency_ms=round((time.perf_counter() - started) * 1000),
                model=self._indexer.embedding_model,
            ),
        )
        runs.extend_lease(run_id, self._clock() + self._lease)
        session.commit()

        run_input = RunInput(
            ticket_id=str(ticket.id),
            repo_connection_id=str(repo_id),
            title=ticket.title,
            description=ticket.description,
            repo_files=self._repo_files.list_files(repo_name, token),
            base_commit_sha=result.commit_sha,
        )
        context = self._contexts.build(
            repo_connection_id=repo_id,
            repo_full_name=repo_name,
            commit_sha=result.commit_sha,
            token=token,
        )
        return run_input, context

    def _renew_lease(self, run_id: UUID) -> None:
        """Keep the run's lease alive from a separate session, so a long indexing job is
        not mistaken for a dead worker."""
        with self._session_factory() as session:
            AgentRunRepository(session).extend_lease(run_id, self._clock() + self._lease)
            session.commit()

    def _finish(
        self,
        session: Session,
        run_id: UUID,
        status: RunStatus,
        ticket_status: TicketStatus,
        error: str | None,
    ) -> None:
        runs = AgentRunRepository(session)
        runs.finish(run_id, status, self._clock(), error)
        run = runs.get(run_id)
        if run is not None:
            TicketRepository(session).set_status([run.ticket_id], ticket_status)
        session.commit()
