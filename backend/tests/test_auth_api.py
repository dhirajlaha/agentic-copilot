from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import GitHubError, OAuthDeniedError
from app.db.models import User, UserSession
from tests.factories import SESSION_COOKIE, create_session_token, create_user

FRONTEND = "https://app.test"


def _start_login(client, **params):
    response = client.get("/auth/github/login", params=params)
    assert response.status_code == 302
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    return response, state


def _failure_code(response) -> str:
    assert response.headers["location"].startswith(f"{FRONTEND}/login?error=")
    return parse_qs(urlsplit(response.headers["location"]).query)["error"][0]


def test_login_redirects_to_github_and_sets_state_cookie(client):
    response, state = _start_login(client)
    assert response.headers["location"].startswith("https://github.test/authorize")
    cookie = response.headers["set-cookie"].lower()
    assert "oauth_state=" in cookie
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert "path=/auth/github" in cookie
    assert state


def test_login_url_carries_pkce_challenge_and_no_scope(client, github):
    response, _ = _start_login(client)
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["code_challenge"] == [github.challenges[-1]]
    assert "scope" not in params


def test_pkce_verifier_is_never_sent_to_the_browser_in_the_url(client):
    response, _ = _start_login(client)
    assert "verifier" not in response.headers["location"]


def test_callback_signs_user_in(client, session: Session, github):
    _, state = _start_login(client, next="/tickets")
    response = client.get("/auth/github/callback", params={"code": "abc", "state": state})

    assert response.status_code == 302
    assert response.headers["location"] == f"{FRONTEND}/tickets"
    assert github.codes_seen == ["abc"]
    cookies = [c.lower() for c in response.headers.get_list("set-cookie")]
    session_cookie = next(c for c in cookies if c.startswith(f"{SESSION_COOKIE.lower()}="))
    assert "httponly" in session_cookie and "secure" in session_cookie
    assert "samesite=lax" in session_cookie and "path=/;" in session_cookie + ";"
    assert "domain=" not in session_cookie  # required for the __Host- prefix
    assert any(c.startswith("oauth_state=") and "max-age=0" in c for c in cookies)  # cleared

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["github_login"] == "octocat"
    assert me.json()["email"] == "octo@example.com"
    assert "token" not in me.text and "github_id" not in me.json()


def test_stored_session_is_a_hash_not_the_cookie_token(client, session: Session):
    _, state = _start_login(client)
    client.get("/auth/github/callback", params={"code": "abc", "state": state})
    raw = client.cookies.get(SESSION_COOKIE)
    stored = session.scalars(select(UserSession.token_hash)).all()
    assert raw and stored and raw not in stored


def test_repeat_login_updates_profile_without_duplicating_user(client, session: Session, github):
    for login in ("octocat", "renamed"):
        github.user = type(github.user)(id=4242, login=login, email=None, avatar_url=None)
        _, state = _start_login(client)
        client.get("/auth/github/callback", params={"code": "c", "state": state})
    users = session.scalars(select(User).where(User.github_id == 4242)).all()
    assert len(users) == 1 and users[0].github_login == "renamed" and users[0].email is None
    assert session.scalar(select(func.count()).select_from(UserSession)) == 2


def test_callback_rejects_wrong_state(client, session: Session):
    _start_login(client)
    response = client.get("/auth/github/callback", params={"code": "abc", "state": "forged"})
    assert _failure_code(response) == "invalid_state"
    assert client.get("/auth/me").status_code == 401
    assert session.scalar(select(func.count()).select_from(UserSession)) == 0


def test_callback_rejects_missing_state_cookie(client):
    response = client.get("/auth/github/callback", params={"code": "abc", "state": "x"})
    assert _failure_code(response) == "invalid_state"


def test_callback_rejects_missing_params(client):
    _start_login(client)
    assert (
        _failure_code(client.get("/auth/github/callback", params={"code": "abc"}))
        == "invalid_request"
    )
    assert _failure_code(client.get("/auth/github/callback")) == "invalid_request"


def test_user_denied_authorization(client):
    _, state = _start_login(client)
    response = client.get(
        "/auth/github/callback", params={"error": "access_denied", "state": state}
    )
    assert _failure_code(response) == "access_denied"


def test_denied_during_token_exchange(client, github):
    github.error = OAuthDeniedError()
    _, state = _start_login(client)
    response = client.get("/auth/github/callback", params={"code": "c", "state": state})
    assert _failure_code(response) == "access_denied"


def test_github_failure_does_not_leak_details(client, github):
    github.error = GitHubError("client_secret leaked?!")
    _, state = _start_login(client)
    response = client.get("/auth/github/callback", params={"code": "c", "state": state})
    assert _failure_code(response) == "github_error"
    assert "secret" not in response.headers["location"]
    assert client.get("/auth/me").status_code == 401


def test_state_cannot_be_replayed_across_login_attempts(client):
    _, first_state = _start_login(client)
    _start_login(client)  # a second attempt replaces the state cookie
    response = client.get("/auth/github/callback", params={"code": "c", "state": first_state})
    assert _failure_code(response) == "invalid_state"


def test_open_redirect_via_next_is_neutralised(client):
    _, state = _start_login(client, next="https://evil.example/steal")
    response = client.get("/auth/github/callback", params={"code": "c", "state": state})
    assert response.headers["location"] == f"{FRONTEND}/"


def test_me_requires_a_session(client):
    response = client.get("/auth/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Cookie"


def test_me_with_unknown_token(client):
    client.cookies.set(SESSION_COOKIE, "not-a-real-token")
    assert client.get("/auth/me").status_code == 401


def test_expired_session_is_rejected(client, session: Session):
    user = create_user(session)
    client.cookies.set(
        "session", create_session_token(session, user, expires_in=timedelta(seconds=-1))
    )
    assert client.get("/auth/me").status_code == 401


def test_logout_revokes_the_session_server_side(client, session: Session):
    user = create_user(session)
    token = create_session_token(session, user)
    client.cookies.set(SESSION_COOKIE, token)
    assert client.get("/auth/me").status_code == 200

    response = client.post("/auth/logout")
    assert response.status_code == 204
    assert f"{SESSION_COOKIE.lower()}=" in response.headers["set-cookie"].lower()  # cleared

    # The old token is dead even if someone kept a copy of the cookie.
    client.cookies.set(SESSION_COOKIE, token)
    assert client.get("/auth/me").status_code == 401


def test_logout_without_session_is_harmless(client):
    assert client.post("/auth/logout").status_code == 204


def test_cross_origin_post_is_blocked(client):
    response = client.post("/auth/logout", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert client.post("/auth/logout", headers={"Origin": FRONTEND}).status_code == 204
    assert client.post("/auth/logout", headers={"Origin": "https://api.test"}).status_code == 204


def test_cors_allows_only_the_frontend(client):
    ok = client.options(
        "/auth/me",
        headers={"Origin": FRONTEND, "Access-Control-Request-Method": "GET"},
    )
    assert ok.headers["access-control-allow-origin"] == FRONTEND
    assert ok.headers["access-control-allow-credentials"] == "true"
    bad = client.options(
        "/auth/me",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in bad.headers


def test_swagger_docs_are_off_by_default(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
