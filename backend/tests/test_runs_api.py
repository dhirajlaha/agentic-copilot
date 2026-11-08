import uuid

import pytest
from sqlalchemy.orm import Session

from tests.factories import (
    SESSION_COOKIE,
    create_repo,
    create_session_token,
    create_ticket,
    create_user,
)
from tests.test_run_executor import make_executor


@pytest.fixture
def alice(session: Session):
    user = create_user(session, "alice")
    ticket = create_ticket(session, user, create_repo(session, user, "octo/demo"))
    return user, ticket, create_session_token(session, user)


@pytest.fixture
def bob(session: Session):
    user = create_user(session, "bob")
    return user, None, create_session_token(session, user)


def _as(client, who):
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, who[2])


def test_starting_a_run_queues_it(client, alice):
    _as(client, alice)
    response = client.post(f"/tickets/{alice[1].id}/run")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending" and body["ticket_id"] == str(alice[1].id)
    assert body["started_at"] is None and body["error"] is None
    assert response.headers["location"] == f"/runs/{body['id']}"
    assert client.get(f"/tickets/{alice[1].id}").json()["status"] == "running"


def test_only_one_active_run_per_ticket(client, alice):
    _as(client, alice)
    assert client.post(f"/tickets/{alice[1].id}/run").status_code == 202
    second = client.post(f"/tickets/{alice[1].id}/run")
    assert second.status_code == 409
    assert "already in progress" in second.json()["detail"]


def test_run_requires_sign_in_and_ownership(client, alice, bob):
    assert client.post(f"/tickets/{alice[1].id}/run").status_code == 401
    _as(client, bob)
    assert client.post(f"/tickets/{alice[1].id}/run").status_code == 404
    assert client.post(f"/tickets/{uuid.uuid4()}/run").status_code == 404


def test_run_detail_is_private(client, alice, bob):
    _as(client, alice)
    run_id = client.post(f"/tickets/{alice[1].id}/run").json()["id"]
    detail = client.get(f"/runs/{run_id}")
    assert detail.status_code == 200 and detail.json()["steps"] == []
    _as(client, bob)
    assert client.get(f"/runs/{run_id}").status_code == 404
    assert client.get(f"/runs/{uuid.uuid4()}").status_code == 404


def test_full_flow_queue_execute_poll_rerun(client, session_factory, alice):
    _as(client, alice)
    run_id = client.post(f"/tickets/{alice[1].id}/run").json()["id"]

    assert str(make_executor(session_factory).run_next()) == run_id  # the worker's job

    detail = client.get(f"/runs/{run_id}").json()
    assert detail["status"] == "completed"
    assert [s["agent_name"] for s in detail["steps"]] == ["indexer", "planner"]
    assert detail["steps"][1]["output"]["subtasks"][0]["title"] == "Fix parser"
    assert client.get(f"/tickets/{alice[1].id}").json()["status"] == "completed"
    # A finished run no longer blocks a new one.
    assert client.post(f"/tickets/{alice[1].id}/run").status_code == 202
