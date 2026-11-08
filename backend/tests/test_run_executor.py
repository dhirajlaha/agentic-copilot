from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.orchestrator import build_graph
from app.agents.schemas import CandidateFileOut, PlannerOutput, SubtaskOut
from app.core.exceptions import RepositoryAccessError
from app.db.enums import RunStatus, TicketStatus
from app.db.models import AgentRun, AgentStep, Ticket
from app.llm.client import LLMUnavailableError
from app.services.run_executor import LEASE_EXPIRED_ERROR, RunExecutor
from tests.factories import create_repo, create_ticket, create_user
from tests.fakes import FakeGitHubApp, FakeLLM, FakeRepoFiles

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def planner_output() -> PlannerOutput:
    return PlannerOutput(
        subtasks=[SubtaskOut(title="Fix parser", details="Allow + in local part")],
        candidate_files=[CandidateFileOut(path="src/login.py", reason="parses emails")],
        open_questions=[],
    )


def make_executor(session_factory, llm=None, repo_files=None, tokens=None, clock=lambda: NOW):
    return RunExecutor(
        session_factory=session_factory,
        graph=build_graph(llm or FakeLLM(planner_output())),
        repo_files=repo_files or FakeRepoFiles(),
        tokens=tokens or FakeGitHubApp(),
        lease_seconds=600,
        clock=clock,
    )


@pytest.fixture
def ticket(session: Session) -> Ticket:
    user = create_user(session)
    return create_ticket(session, user, create_repo(session, user, "octo/demo"))


def queue_run(session: Session, ticket: Ticket, **values) -> AgentRun:
    run = AgentRun(ticket_id=ticket.id, **values)
    session.add(run)
    session.flush()
    return run


def reload(session: Session, obj):
    session.expire_all()
    return session.get(type(obj), obj.id)


def test_idle_worker_returns_none(session_factory):
    assert make_executor(session_factory).run_next() is None


def test_successful_run_persists_steps_and_completes(session, session_factory, ticket):
    run = queue_run(session, ticket)
    session.commit()
    repo_files = FakeRepoFiles()
    app = FakeGitHubApp()

    assert make_executor(session_factory, repo_files=repo_files, tokens=app).run_next() == run.id

    run = reload(session, run)
    assert run.status == RunStatus.COMPLETED
    assert run.started_at == NOW and run.finished_at == NOW
    assert run.lease_expires_at is None and run.error is None
    assert reload(session, ticket).status == TicketStatus.COMPLETED
    assert repo_files.requested == ["octo/demo"]
    # The files were read with a token minted for this repo's installation, not anonymously.
    installation_id = ticket.repo_connection.github_installation.installation_id
    assert app.tokens_requested == [installation_id]
    assert repo_files.tokens == [f"token-for-{installation_id}"]

    (step,) = session.scalars(select(AgentStep).where(AgentStep.run_id == run.id)).all()
    assert step.agent_name == "planner"
    assert step.tokens_used == 150 and step.latency_ms == 42
    assert step.output["candidate_files"][0]["path"] == "src/login.py"
    assert step.input == {"ticket_id": str(ticket.id), "files_shown": 2, "files_omitted": 0}


def test_oldest_pending_run_is_claimed_first(session, session_factory):
    user = create_user(session)
    repo = create_repo(session, user)
    first = queue_run(
        session, create_ticket(session, user, repo), created_at=NOW - timedelta(minutes=5)
    )
    queue_run(session, create_ticket(session, user, repo), created_at=NOW)
    session.commit()
    assert make_executor(session_factory).run_next() == first.id


def test_running_runs_are_not_claimed_again(session, session_factory, ticket):
    queue_run(
        session, ticket, status=RunStatus.RUNNING, lease_expires_at=NOW + timedelta(minutes=5)
    )
    session.commit()
    assert make_executor(session_factory).run_next() is None


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"repo_files": FakeRepoFiles(error=RepositoryAccessError("octo/demo is private"))},
            "octo/demo is private",
        ),
        (
            {"llm": FakeLLM(error=LLMUnavailableError("anthropic: rate limited"))},
            "anthropic: rate limited",
        ),
    ],
)
def test_known_failures_are_recorded_with_their_message(
    session, session_factory, ticket, kwargs, message
):
    run = queue_run(session, ticket)
    session.commit()
    make_executor(session_factory, **kwargs).run_next()

    run = reload(session, run)
    assert run.status == RunStatus.FAILED and run.error == message
    assert run.finished_at == NOW and run.lease_expires_at is None
    assert reload(session, ticket).status == TicketStatus.OPEN


def test_unexpected_errors_are_not_leaked_to_users(session, session_factory, ticket):
    run = queue_run(session, ticket)
    session.commit()
    make_executor(
        session_factory, llm=FakeLLM(error=RuntimeError("db password=hunter2"))
    ).run_next()

    run = reload(session, run)
    assert run.status == RunStatus.FAILED
    assert run.error == "Internal error while running the pipeline."
    assert "hunter2" not in run.error


def test_runs_with_expired_leases_are_reaped(session, session_factory, ticket):
    user = ticket.user
    healthy_ticket = create_ticket(session, user, ticket.repo_connection)
    dead = queue_run(
        session, ticket, status=RunStatus.RUNNING, lease_expires_at=NOW - timedelta(seconds=1)
    )
    alive = queue_run(
        session,
        healthy_ticket,
        status=RunStatus.RUNNING,
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    ticket.status = TicketStatus.RUNNING
    session.commit()

    assert make_executor(session_factory).reap_expired() == 1

    dead, alive = reload(session, dead), reload(session, alive)
    assert dead.status == RunStatus.FAILED and dead.error == LEASE_EXPIRED_ERROR
    assert reload(session, ticket).status == TicketStatus.OPEN
    assert alive.status == RunStatus.RUNNING
