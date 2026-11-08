import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import GitHubError
from app.db.models import GitHubInstallation, RepoConnection, Ticket, User
from app.integrations.github import GitHubUser, InstallationInfo
from app.integrations.github_app import InstallationRepo
from tests.factories import (
    SESSION_COOKIE,
    create_installation,
    create_repo,
    create_session_token,
    create_ticket,
    create_user,
)

FRONTEND = "https://app.test"


@pytest.fixture
def alice(session: Session, github) -> tuple[User, str]:
    user = create_user(session, "alice")
    token = create_session_token(session, user)
    # FakeGitHub authorises whoever the test says is "on GitHub": make that Alice.
    github.user = GitHubUser(id=user.github_id, login="alice", email=None, avatar_url=None)
    return user, token


@pytest.fixture
def bob(session: Session) -> tuple[User, str]:
    user = create_user(session, "bob")
    return user, create_session_token(session, user)


def _as(client, who):
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, who[1])


def _start_install(client, **params):
    response = client.get("/repos/install", params=params)
    assert response.status_code == 302
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    return response, state


def _callback(client, state, **params):
    return client.get("/auth/github/callback", params={"code": "c0de", "state": state, **params})


def _error(response) -> str:
    assert response.headers["location"].startswith(f"{FRONTEND}/repos?error=")
    return parse_qs(urlsplit(response.headers["location"]).query)["error"][0]


# ---- starting the install -------------------------------------------------------------


def test_install_requires_sign_in(client):
    assert client.get("/repos/install").status_code == 401


def test_install_redirects_to_github_with_a_bound_state_cookie(client, alice):
    _as(client, alice)
    response, state = _start_install(client)
    assert response.headers["location"].startswith(
        "https://github.test/apps/test-app/installations/new"
    )
    cookie = response.headers["set-cookie"].lower()
    assert "oauth_state=" in cookie and "httponly" in cookie and "secure" in cookie
    assert "path=/auth/github" in cookie
    assert state


# ---- finishing the install ------------------------------------------------------------


def test_callback_links_a_verified_installation(client, session, github, alice):
    github.installations = [InstallationInfo(555, "octo-org", "Organization")]
    _as(client, alice)
    _, state = _start_install(client)

    response = _callback(client, state, installation_id="555", setup_action="install")

    assert response.status_code == 302
    assert response.headers["location"] == f"{FRONTEND}/repos"
    row = session.scalars(select(GitHubInstallation)).one()
    assert (row.user_id, row.installation_id, row.account_login) == (
        alice[0].id,
        555,
        "octo-org",
    )
    cookies = [c.lower() for c in response.headers.get_list("set-cookie")]
    assert any(c.startswith("oauth_state=") and "max-age=0" in c for c in cookies)  # cleared


def test_callback_honours_a_safe_next_path_only(client, github, alice):
    github.installations = [InstallationInfo(1, "o", "User")]
    _as(client, alice)
    _, state = _start_install(client, next="/tickets/new")
    assert _callback(client, state, installation_id="1").headers["location"] == (
        f"{FRONTEND}/tickets/new"
    )
    _, state = _start_install(client, next="//evil.test")
    assert _callback(client, state, installation_id="1").headers["location"] == (
        f"{FRONTEND}/repos"
    )


def test_relinking_the_same_installation_does_not_duplicate(client, session, github, alice):
    github.installations = [InstallationInfo(7, "old-name", "User")]
    _as(client, alice)
    for name in ("old-name", "new-name"):
        github.installations = [InstallationInfo(7, name, "User")]
        _, state = _start_install(client)
        _callback(client, state, installation_id="7")
    row = session.scalars(select(GitHubInstallation)).one()
    assert row.account_login == "new-name"


def test_installation_id_not_owned_by_the_user_is_rejected(client, session, github, alice):
    """The core attack: edit installation_id in the URL to someone else's installation."""
    github.installations = [InstallationInfo(555, "octo-org", "Organization")]
    _as(client, alice)
    _, state = _start_install(client)

    response = _callback(client, state, installation_id="999")

    assert _error(response) == "installation_not_verified"
    assert session.scalar(select(func.count()).select_from(GitHubInstallation)) == 0


def test_authorising_a_different_github_account_is_rejected(client, session, github, alice):
    github.installations = [InstallationInfo(555, "octo-org", "Organization")]
    github.grant_user_id = alice[0].github_id + 1  # someone else clicked through
    _as(client, alice)
    _, state = _start_install(client)

    assert _error(_callback(client, state, installation_id="555")) == ("installation_not_verified")
    assert session.scalar(select(func.count()).select_from(GitHubInstallation)) == 0


def test_callback_requires_a_signed_in_user(client, github, alice):
    github.installations = [InstallationInfo(555, "o", "User")]
    _as(client, alice)
    _, state = _start_install(client)
    client.cookies.clear()  # session lost before GitHub sent the browser back
    assert _error(_callback(client, state, installation_id="555")) == "not_signed_in"


def test_another_users_state_cannot_finish_my_install(client, github, alice, bob):
    github.installations = [InstallationInfo(555, "o", "User")]
    _as(client, alice)
    _, state = _start_install(client)  # Alice's state cookie stays in the jar
    client.cookies.set(SESSION_COOKIE, bob[1])  # ...but Bob is the one signed in
    assert _error(_callback(client, state, installation_id="555")) == "invalid_state"


def test_wrong_or_missing_state_is_rejected(client, alice):
    _as(client, alice)
    _start_install(client)
    assert _error(_callback(client, "forged", installation_id="1")) == "invalid_state"
    client.cookies.delete("oauth_state", path="/auth/github")
    assert _error(_callback(client, "x", installation_id="1")) == "invalid_state"


def test_a_login_state_cannot_be_used_for_an_install(client, github, alice):
    github.installations = [InstallationInfo(555, "o", "User")]
    _as(client, alice)
    login = client.get("/auth/github/login")
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
    assert _error(_callback(client, state, installation_id="555")) == "invalid_state"


def test_an_install_state_cannot_be_used_to_sign_in(client, session, alice):
    _as(client, alice)
    _, state = _start_install(client)
    response = client.get("/auth/github/callback", params={"code": "c", "state": state})
    assert response.headers["location"].startswith(f"{FRONTEND}/login?error=invalid_state")


def test_pending_approval_and_denial_and_junk(client, alice):
    _as(client, alice)
    _, state = _start_install(client)
    # An organisation owner must approve first: GitHub sends setup_action=request, no id.
    assert _error(_callback(client, state, setup_action="request")) == "installation_pending"
    assert _error(_callback(client, state, installation_id="abc")) == "invalid_request"
    assert _error(_callback(client, state, installation_id="1" * 20)) == "invalid_request"
    denied = client.get(
        "/auth/github/callback",
        params={"error": "access_denied", "state": state, "setup_action": "install"},
    )
    assert _error(denied) == "access_denied"


def test_github_failure_during_verification_is_reported(client, github, alice):
    github.error = GitHubError("boom")
    _as(client, alice)
    _, state = _start_install(client)
    assert _error(_callback(client, state, installation_id="1")) == "github_error"


# ---- choosing repositories --------------------------------------------------------------


def test_available_lists_repos_per_installation(client, session, github_app, alice):
    inst = create_installation(session, alice[0], installation_id=10, account_login="octo-org")
    gone = create_installation(session, alice[0], installation_id=11, account_login="old")
    github_app.repos[10] = [InstallationRepo("octo/a", True), InstallationRepo("octo/b", False)]
    github_app.unreachable.add(11)
    _as(client, alice)

    body = {i["account_login"]: i for i in client.get("/repos/available").json()}

    assert body["octo-org"]["id"] == str(inst.id) and body["octo-org"]["reachable"] is True
    assert body["octo-org"]["repos"] == [
        {"full_name": "octo/a", "private": True},
        {"full_name": "octo/b", "private": False},
    ]
    assert body["old"]["id"] == str(gone.id)
    assert body["old"]["reachable"] is False and body["old"]["repos"] == []


def test_connect_repo_uses_githubs_view_and_canonical_name(client, session, github_app, alice):
    inst = create_installation(session, alice[0], installation_id=10)
    github_app.repos[10] = [InstallationRepo("Octo/Demo", True)]
    _as(client, alice)

    response = client.post(
        "/repos", json={"installation_id": str(inst.id), "full_name": "octo/demo"}
    )

    assert response.status_code == 201
    assert response.json()["repo_full_name"] == "Octo/Demo"
    row = session.scalars(select(RepoConnection)).one()
    assert row.user_id == alice[0].id and row.github_installation_id == inst.id


def test_connect_rejects_repos_the_installation_cannot_see(client, session, github_app, alice):
    inst = create_installation(session, alice[0], installation_id=10)
    github_app.repos[10] = [InstallationRepo("octo/demo", True)]
    _as(client, alice)
    response = client.post(
        "/repos", json={"installation_id": str(inst.id), "full_name": "octo/secret"}
    )
    assert response.status_code == 400
    assert session.scalar(select(func.count()).select_from(RepoConnection)) == 0


@pytest.mark.parametrize("name", ["no-slash", "../../etc", "a/b/c", "owner/repo name"])
def test_connect_rejects_malformed_names(client, session, alice, name):
    inst = create_installation(session, alice[0])
    _as(client, alice)
    response = client.post("/repos", json={"installation_id": str(inst.id), "full_name": name})
    assert response.status_code in (400, 422)


def test_connect_twice_is_a_conflict(client, session, github_app, alice):
    inst = create_installation(session, alice[0], installation_id=10)
    github_app.repos[10] = [InstallationRepo("octo/demo", False)]
    _as(client, alice)
    body = {"installation_id": str(inst.id), "full_name": "octo/demo"}
    assert client.post("/repos", json=body).status_code == 201
    assert client.post("/repos", json=body).status_code == 409


def test_cannot_connect_through_someone_elses_installation(client, session, github_app, alice, bob):
    theirs = create_installation(session, bob[0], installation_id=10)
    github_app.repos[10] = [InstallationRepo("octo/demo", False)]
    _as(client, alice)
    response = client.post(
        "/repos", json={"installation_id": str(theirs.id), "full_name": "octo/demo"}
    )
    assert response.status_code == 404
    assert (
        client.post(
            "/repos", json={"installation_id": str(uuid.uuid4()), "full_name": "octo/demo"}
        ).status_code
        == 404
    )


def test_list_is_scoped_to_the_user(client, session, alice, bob):
    mine = create_repo(session, alice[0], "octo/mine")
    create_repo(session, bob[0], "octo/theirs")
    _as(client, alice)
    assert [r["id"] for r in client.get("/repos").json()] == [str(mine.id)]


def test_disconnect_deletes_the_repo_and_its_tickets(client, session, alice):
    repo = create_repo(session, alice[0])
    ticket = create_ticket(session, alice[0], repo)
    repo_id, ticket_id = repo.id, ticket.id
    _as(client, alice)
    assert client.delete(f"/repos/{repo_id}").status_code == 204
    session.expire_all()
    assert session.get(RepoConnection, repo_id) is None
    assert session.get(Ticket, ticket_id) is None
    # The installation link itself stays, so the repo can be connected again.
    assert session.scalar(select(func.count()).select_from(GitHubInstallation)) == 1


def test_disconnect_is_tenant_scoped(client, session, alice, bob):
    theirs = create_repo(session, bob[0])
    _as(client, alice)
    assert client.delete(f"/repos/{theirs.id}").status_code == 404
    assert session.get(RepoConnection, theirs.id) is not None


def test_repo_routes_require_sign_in(client):
    rid = uuid.uuid4()
    assert client.get("/repos").status_code == 401
    assert client.get("/repos/available").status_code == 401
    assert (
        client.post("/repos", json={"installation_id": str(rid), "full_name": "a/b"}).status_code
        == 401
    )
    assert client.delete(f"/repos/{rid}").status_code == 401
