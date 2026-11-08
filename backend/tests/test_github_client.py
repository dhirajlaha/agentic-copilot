import httpx
import pytest

from app.core.exceptions import GitHubError, OAuthDeniedError
from app.integrations.github import GitHubOAuthClient


def make_client(handler) -> GitHubOAuthClient:
    return GitHubOAuthClient(
        client_id="cid",
        client_secret="secret",
        redirect_uri="https://api.test/auth/github/callback",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_authorization_url_contains_required_params():
    url = httpx.URL(make_client(lambda r: httpx.Response(500)).authorization_url("st4te", "chal"))
    assert url.host == "github.com"
    params = dict(url.params)
    assert params["client_id"] == "cid"
    assert params["state"] == "st4te"
    assert params["redirect_uri"] == "https://api.test/auth/github/callback"
    assert params["code_challenge"] == "chal"
    assert params["code_challenge_method"] == "S256"
    assert "scope" not in params


def test_fetch_user_uses_profile_email():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if request.url.path == "/login/oauth/access_token":
            body = request.content.decode()
            assert "code=thecode" in body and "client_secret=secret" in body
            assert "code_verifier=ver1fier" in body
            return httpx.Response(200, json={"access_token": "tok"})
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(
            200,
            json={
                "id": 7,
                "login": "octo",
                "email": "o@x.io",
                "avatar_url": "https://a",
            },
        )

    user = make_client(handler).fetch_user("thecode", "ver1fier")
    assert (user.id, user.login, user.email) == (7, "octo", "o@x.io")
    assert not any("/user/emails" in u for _, u in seen)


def test_fetch_user_falls_back_to_primary_verified_email():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "tok"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 7, "login": "octo", "email": None})
        return httpx.Response(
            200,
            json=[
                {"email": "unverified@x.io", "primary": True, "verified": False},
                {"email": "other@x.io", "primary": False, "verified": True},
                {"email": "main@x.io", "primary": True, "verified": True},
            ],
        )

    assert make_client(handler).fetch_user("c", "v").email == "main@x.io"


def test_bad_code_is_a_github_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "bad_verification_code"})

    with pytest.raises(GitHubError):
        make_client(handler).fetch_user("c", "v")


def test_access_denied_is_distinguished():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "access_denied"})

    with pytest.raises(OAuthDeniedError):
        make_client(handler).fetch_user("c", "v")


@pytest.mark.parametrize("status", [401, 500])
def test_http_errors_become_github_errors(status):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(status)

    with pytest.raises(GitHubError):
        make_client(handler).fetch_user("c", "v")


def test_network_failure_becomes_github_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(GitHubError):
        make_client(handler).fetch_user("c", "v")


def test_malformed_profile_is_a_github_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json={"unexpected": True})

    with pytest.raises(GitHubError):
        make_client(handler).fetch_user("c", "v")


def test_email_lookup_failure_does_not_block_sign_in():
    """Without the email permission GitHub answers 403; we sign in without an email."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "tok"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 7, "login": "octo", "email": None})
        return httpx.Response(403, json={"message": "Resource not accessible"})

    user = make_client(handler).fetch_user("c", "v")
    assert (user.id, user.login, user.email) == (7, "octo", None)


def installations_handler(pages, seen=None):
    """Token exchange, /user, then /user/installations served page by page."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            if seen is not None:
                seen.append(request.content.decode())
            return httpx.Response(200, json={"access_token": "tok"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 7, "login": "octo"})
        assert request.url.path == "/user/installations"
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"installations": pages[page - 1]})

    return handler


def installation(n: int) -> dict:
    return {"id": n, "account": {"login": f"acct{n}", "type": "Organization"}}


def test_fetch_installations_returns_user_and_installations():
    seen: list[str] = []
    grant = make_client(
        installations_handler([[installation(1), installation(2)]], seen)
    ).fetch_installations("c0de")
    assert grant.github_user_id == 7
    assert [(i.id, i.account_login, i.account_type) for i in grant.installations] == [
        (1, "acct1", "Organization"),
        (2, "acct2", "Organization"),
    ]
    # The install flow has no PKCE challenge, so no verifier is sent.
    assert "code_verifier" not in seen[0] and "client_secret=secret" in seen[0]


def test_fetch_installations_follows_pages():
    first = [installation(n) for n in range(100)]
    grant = make_client(installations_handler([first, [installation(500)]])).fetch_installations(
        "c"
    )
    assert len(grant.installations) == 101


def test_fetch_installations_rejects_malformed_responses():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "tok"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 7})
        return httpx.Response(200, json={"installations": [{"id": 1}]})

    with pytest.raises(GitHubError):
        make_client(handler).fetch_installations("c")
