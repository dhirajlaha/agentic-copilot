import uuid

import pytest

from app.api.deps import SessionDep, get_publish_service
from app.db.enums import RunStatus
from app.db.models import AgentRun, AgentStep
from app.integrations.github_publish import PublishedPullRequest
from app.repositories.run_repository import AgentRunRepository
from app.services.publish_service import PublishService
from tests.factories import (
    SESSION_COOKIE,
    create_repo,
    create_session_token,
    create_ticket,
    create_user,
)
from tests.fakes import FakeFileReader, FakeGitHubApp, FakePublisher
from tests.test_publish_service import LOGIN, make_patch


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture(autouse=True)
def _wire_fakes(client, publisher):
    # Reuse the real dependency (so it gets the per-request session) but swap in the fakes
    # for everything that would talk to GitHub.
    def override(session: SessionDep) -> PublishService:
        return PublishService(
            session=session,
            runs=AgentRunRepository(session),
            tokens=FakeGitHubApp(),
            files=FakeFileReader({"src/login.py": LOGIN}),
            publisher=publisher,
        )

    client.app.dependency_overrides[get_publish_service] = override


@pytest.fixture
def alice(session):
    user = create_user(session, "alice")
    ticket = create_ticket(session, user, create_repo(session, user, "octo/demo"), "Fix login")
    run = AgentRun(ticket_id=ticket.id, status=RunStatus.COMPLETED)
    session.add(run)
    session.flush()
    session.add(
        AgentStep(
            run_id=run.id, agent_name="coder", input={}, output=make_patch().model_dump(mode="json")
        )
    )
    token = create_session_token(session, user)
    session.commit()
    return user, run, token


def as_user(client, token):
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, token)


def test_publishing_requires_sign_in(client):
    assert client.post(f"/runs/{uuid.uuid4()}/publish").status_code == 401


def test_publishing_returns_the_pull_request(client, alice, publisher):
    as_user(client, alice[2])
    response = client.post(f"/runs/{alice[1].id}/publish")

    assert response.status_code == 200
    body = response.json()
    assert body["publication_status"] == "published"
    assert body["pull_request_url"] == "https://github.com/octo/demo/pull/41"
    assert body["pull_request_number"] == 41
    assert body["branch_name"].startswith("copilot/fix-login-")
    assert body["publish_error"] is None
    assert len(publisher.calls) == 1


def test_the_pull_request_shows_on_the_run(client, alice):
    as_user(client, alice[2])
    client.post(f"/runs/{alice[1].id}/publish")
    run = client.get(f"/runs/{alice[1].id}").json()
    assert run["pull_request_url"] == "https://github.com/octo/demo/pull/41"
    assert run["publication_status"] == "published"


def test_publishing_again_is_safe(client, alice, publisher):
    as_user(client, alice[2])
    first = client.post(f"/runs/{alice[1].id}/publish").json()
    second = client.post(f"/runs/{alice[1].id}/publish").json()
    assert first["pull_request_url"] == second["pull_request_url"]
    assert len(publisher.calls) == 1


def test_someone_elses_run_is_not_found(client, session, alice, publisher):
    stranger = create_session_token(session, create_user(session, "mallory"))
    session.commit()
    as_user(client, stranger)
    assert client.post(f"/runs/{alice[1].id}/publish").status_code == 404
    assert publisher.calls == []


def test_a_run_that_is_not_finished_conflicts(client, session, alice):
    alice[1].status = RunStatus.RUNNING
    session.commit()
    as_user(client, alice[2])
    response = client.post(f"/runs/{alice[1].id}/publish")
    assert response.status_code == 409 and "completed" in response.json()["detail"]


def test_a_github_failure_is_reported_and_recorded(client, alice, publisher):
    from app.core.exceptions import RepositoryAccessError

    publisher.error = RepositoryAccessError("GitHub refused to let the app write")
    as_user(client, alice[2])
    response = client.post(f"/runs/{alice[1].id}/publish")
    assert response.status_code == 400
    assert response.json()["detail"] == "GitHub refused to let the app write"
    run = client.get(f"/runs/{alice[1].id}").json()
    assert run["publication_status"] == "failed"
    assert run["publish_error"] == "GitHub refused to let the app write"


def test_a_new_pull_request_type_is_importable():
    assert PublishedPullRequest("b", 1, "u").number == 1
