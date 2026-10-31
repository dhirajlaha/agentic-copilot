import uuid

import pytest
from sqlalchemy.orm import Session

from app.db.models import RepoConnection, User
from tests.factories import SESSION_COOKIE, create_repo, create_session_token, create_user


@pytest.fixture
def alice(session: Session) -> tuple[User, RepoConnection, str]:
    user = create_user(session, "alice")
    return user, create_repo(session, user), create_session_token(session, user)


@pytest.fixture
def bob(session: Session) -> tuple[User, RepoConnection, str]:
    user = create_user(session, "bob")
    return user, create_repo(session, user), create_session_token(session, user)


def _as(client, who):
    """Authenticate the shared client as `who` (a fixture tuple) by setting its cookie."""
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, who[2])


def _create(client, who, title="Fix login bug", **extra):
    _as(client, who)
    return client.post(
        "/tickets", json={"title": title, "repo_connection_id": str(who[1].id), **extra}
    )


def test_all_routes_require_sign_in(client):
    tid = uuid.uuid4()
    calls = [
        client.get("/tickets"),
        client.post("/tickets", json={"title": "x", "repo_connection_id": str(tid)}),
        client.get(f"/tickets/{tid}"),
        client.patch(f"/tickets/{tid}", json={"title": "x"}),
        client.delete(f"/tickets/{tid}"),
    ]
    assert [c.status_code for c in calls] == [401] * 5


def test_create_and_get(client, alice):
    r = _create(client, alice, description="steps to reproduce")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "open"
    assert body["repo_connection_id"] == str(alice[1].id)
    got = client.get(f"/tickets/{body['id']}")
    assert got.status_code == 200 and got.json()["description"] == "steps to reproduce"


def test_create_validation(client, alice):
    assert _create(client, alice, title="").status_code == 422
    _as(client, alice)
    missing = client.post("/tickets", json={"title": "x", "repo_connection_id": str(uuid.uuid4())})
    assert missing.status_code == 404


def test_cannot_use_someone_elses_repo(client, alice, bob):
    _as(client, alice)
    r = client.post("/tickets", json={"title": "x", "repo_connection_id": str(bob[1].id)})
    assert r.status_code == 404


def test_tenant_isolation(client, alice, bob):
    ticket_id = _create(client, alice).json()["id"]
    _as(client, bob)
    assert client.get("/tickets").json() == []
    for call in (
        client.get(f"/tickets/{ticket_id}"),
        client.patch(f"/tickets/{ticket_id}", json={"title": "x"}),
        client.delete(f"/tickets/{ticket_id}"),
    ):
        assert call.status_code == 404
    _as(client, alice)
    assert client.get(f"/tickets/{ticket_id}").status_code == 200


def test_list_filters_and_pagination(client, session: Session, alice):
    user, _, _ = alice
    other_repo = create_repo(session, user, "octo/second")
    for i in range(3):
        _create(client, alice, title=f"t{i}")
    _as(client, alice)
    client.post("/tickets", json={"title": "other", "repo_connection_id": str(other_repo.id)})

    assert len(client.get("/tickets").json()) == 4
    by_repo = client.get("/tickets", params={"repo_connection_id": str(other_repo.id)})
    assert [t["title"] for t in by_repo.json()] == ["other"]
    assert len(client.get("/tickets?limit=2").json()) == 2
    assert len(client.get("/tickets?limit=2&offset=2").json()) == 2
    assert client.get("/tickets?status=running").json() == []
    assert len(client.get("/tickets?status=open").json()) == 4
    assert client.get("/tickets?status=bogus").status_code == 422
    assert client.get("/tickets?limit=0").status_code == 422


def test_update_is_partial_and_cannot_set_status(client, alice):
    tid = _create(client, alice, description="keep me").json()["id"]
    r = client.patch(f"/tickets/{tid}", json={"title": "New title", "status": "completed"})
    assert r.status_code == 200
    assert r.json()["title"] == "New title"
    assert r.json()["description"] == "keep me"
    assert r.json()["status"] == "open"  # unknown field ignored
    assert client.patch(f"/tickets/{tid}", json={"title": None}).status_code == 422
    assert client.patch(f"/tickets/{tid}", json={}).status_code == 200  # no-op is fine


def test_delete(client, alice):
    tid = _create(client, alice).json()["id"]
    assert client.delete(f"/tickets/{tid}").status_code == 204
    assert client.get(f"/tickets/{tid}").status_code == 404
    assert client.delete(f"/tickets/{tid}").status_code == 404
