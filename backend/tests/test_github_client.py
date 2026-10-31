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
