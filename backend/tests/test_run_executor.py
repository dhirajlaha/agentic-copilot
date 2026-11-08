from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.orchestrator import build_graph
from app.agents.schemas import CandidateFileOut, CoderOutput, PlannerOutput, SubtaskOut
from app.core.exceptions import RepositoryAccessError
from app.db.enums import RunStatus, TicketStatus
from app.db.models import AgentRun, AgentStep, Ticket
from app.llm.client import LLMUnavailableError
from app.services.run_executor import LEASE_EXPIRED_ERROR, RunExecutor
from tests.factories import create_repo, create_ticket, create_user
from tests.fakes import (
    FakeContextBuilder,
    FakeGitHubApp,
    FakeIndexer,
    FakeLLM,
    FakeRepoFiles,
    default_coder_output,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def planner_output() -> PlannerOutput:
    return PlannerOutput(
        subtasks=[SubtaskOut(title="Fix parser", details="Allow + in local part")],
        candidate_files=[CandidateFileOut(path="src/login.py", reason="parses emails")],
        open_questions=[],
    )


def default_llm() -> FakeLLM:
    return FakeLLM(outputs={PlannerOutput: planner_output(), CoderOutput: default_coder_output()})


def make_executor(
    session_factory,
    llm=None,
    repo_files=None,
    tokens=None,
    indexer=None,
    contexts=None,
    clock=lambda: NOW,
):
    return RunExecutor(
        session_factory=session_factory,
        graph=build_graph(llm or default_llm()),
        repo_files=repo_files or FakeRepoFiles(),
        tokens=tokens or FakeGitHubApp(),
        indexer=indexer or FakeIndexer(),
        contexts=contexts or FakeContextBuilder(),
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

    rows = session.scalars(select(AgentStep).where(AgentStep.run_id == run.id)).all()
    order = {"indexer": 0, "planner": 1, "coder": 2}
    indexer_step, step, coder_step = sorted(rows, key=lambda r: order[r.agent_name])
    assert indexer_step.agent_name == "indexer"
    assert step.agent_name == "planner"
    assert coder_step.agent_name == "coder"
    assert coder_step.output["files_changed"] == 1
    assert coder_step.output["base_commit_sha"] == "b" * 40  # the commit the index synced to
    assert step.tokens_used == 150 and step.latency_ms == 42
    assert step.output["candidate_files"][0]["path"] == "src/login.py"
    assert step.input == {"ticket_id": str(ticket.id), "files_shown": 2, "files_omitted": 0}


def test_the_index_is_synced_first_and_recorded_as_a_step(session, session_factory, ticket):
    run = queue_run(session, ticket)
    session.commit()
    indexer = FakeIndexer()
    app = FakeGitHubApp()

    make_executor(session_factory, indexer=indexer, tokens=app).run_next()

    installation_id = ticket.repo_connection.github_installation.installation_id
    assert indexer.calls == [{"repo": "octo/demo", "token": f"token-for-{installation_id}"}]
    step = session.scalars(
        select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.agent_name == "indexer")
    ).one()
    assert step.input == {"repo_connection_id": str(ticket.repo_connection_id)}
    assert step.output["commit_sha"] == "b" * 40
    assert step.output["files_indexed"] == 3 and step.output["chunks_written"] == 5
    assert step.tokens_used is None


def test_a_failing_index_fails_the_run_before_any_agent_runs(session, session_factory, ticket):
    run = queue_run(session, ticket)
    session.commit()
    llm = default_llm()
    indexer = FakeIndexer(error=RepositoryAccessError("octo/demo is too large to index"))

    make_executor(session_factory, llm=llm, indexer=indexer).run_next()

    run = reload(session, run)
    assert run.status == RunStatus.FAILED
    assert run.error == "octo/demo is too large to index"
    assert llm.calls == []  # the Planner never ran
    assert reload(session, ticket).status == TicketStatus.OPEN


def test_the_indexers_heartbeat_renews_the_run_lease(session, session_factory, ticket):
    run = queue_run(session, ticket)
    session.commit()
    executor = make_executor(session_factory, indexer=FakeIndexer())  # FakeIndexer beats once
    renewed = []
    original = executor._renew_lease
    executor._renew_lease = lambda run_id: (renewed.append(run_id), original(run_id))[1]

    executor.run_next()

    assert renewed == [run.id]


def test_the_run_context_is_built_for_the_synced_commit_with_the_run_token(
    session, session_factory, ticket
):
    queue_run(session, ticket)
    session.commit()
    contexts = FakeContextBuilder()
    app = FakeGitHubApp()

    make_executor(session_factory, contexts=contexts, tokens=app).run_next()

    installation_id = ticket.repo_connection.github_installation.installation_id
    assert contexts.built == [
        {"repo": "octo/demo", "commit": "b" * 40, "token": f"token-for-{installation_id}"}
    ]


def test_a_coder_that_cannot_produce_a_valid_patch_fails_the_run_clearly(
    session, session_factory, ticket
):
    from app.agents.schemas import EditOut, FileChangeOut

    run = queue_run(session, ticket)
    session.commit()
    bad = CoderOutput(
        summary="x",
        changes=[
            FileChangeOut(
                path="src/login.py",
                action="modify",
                edits=[EditOut(search="text that is not in the file", replace="y")],
                content="",
                reason="r",
            )
        ],
        open_questions=[],
    )
    llm = FakeLLM(outputs={PlannerOutput: planner_output(), CoderOutput: [bad, bad, bad]})

    make_executor(session_factory, llm=llm).run_next()

    run = reload(session, run)
    assert run.status == RunStatus.FAILED
    assert "could not produce a valid patch after 3 attempts" in run.error
    assert "was not found in the file" in run.error
    assert reload(session, ticket).status == TicketStatus.OPEN


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
