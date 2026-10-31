"""Smoke tests for the schema. Needs the local Postgres container (see README.md)
with migrations applied (`uv run alembic upgrade head`). Each test runs inside a
transaction that is rolled back, so the database is left untouched."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.enums import RunStatus, TicketStatus
from app.db.models import (
    AgentRun,
    AgentStep,
    CodeChunk,
    RepoConnection,
    Ticket,
    User,
)


def _make_ticket(session: Session) -> Ticket:
    user = User(github_id=1, github_login="dev", email="dev@example.com")
    repo = RepoConnection(user=user, repo_full_name="octo/demo")
    ticket = Ticket(user=user, repo_connection=repo, title="Fix bug", description="details")
    session.add(ticket)
    session.flush()
    return ticket


def test_defaults_are_applied(session: Session):
    ticket = _make_ticket(session)
    session.refresh(ticket)
    assert ticket.id is not None
    assert ticket.created_at is not None
    assert ticket.status == TicketStatus.OPEN


def test_run_steps_and_vector_roundtrip(session: Session):
    ticket = _make_ticket(session)
    run = AgentRun(ticket=ticket)
    run.steps.append(AgentStep(agent_name="planner", input={"a": 1}, output={"plan": ["x"]}))
    session.add(run)
    session.add(
        CodeChunk(
            repo_connection=ticket.repo_connection,
            file_path="a.py",
            content="print(1)",
            embedding=[0.1] * 384,
        )
    )
    session.flush()
    session.refresh(run)
    assert run.status == RunStatus.PENDING
    assert run.steps[0].output == {"plan": ["x"]}
    chunk = session.scalars(select(CodeChunk)).one()
    assert len(chunk.embedding) == 384


def test_deleting_repo_cascades(session: Session):
    ticket = _make_ticket(session)
    repo = ticket.repo_connection
    session.add(AgentRun(ticket=ticket))
    session.flush()
    session.delete(repo)
    session.flush()
    assert session.scalars(select(Ticket)).all() == []
    assert session.scalars(select(AgentRun)).all() == []


def test_invalid_status_is_rejected_by_database(session: Session):
    ticket = _make_ticket(session)
    session.add(AgentRun(ticket=ticket, status="runing"))
    with pytest.raises(IntegrityError, match="ck_agent_runs_status"):
        session.flush()
