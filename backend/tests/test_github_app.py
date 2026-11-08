"""The GitHub App client: signs a short-lived JWT and mints installation tokens."""

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from app.core.exceptions import GitHubError, RepositoryAccessError
from app.integrations.github_app import GitHubAppClient
from tests.conftest import TEST_APP_KEY

PRIVATE_PEM = TEST_APP_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()


def make_client(handler) -> GitHubAppClient:
    return GitHubAppClient(
        client_id="Iv1.client",
        private_key_pem=PRIVATE_PEM,
        slug="my-app",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_install_url_is_the_apps_install_page():
    url = httpx.URL(make_client(lambda r: httpx.Response(500)).install_url())
    assert url.host == "github.com" and url.path == "/apps/my-app/installations/new"
    assert not url.params  # no state: the redirect comes back via the Setup URL


def test_app_jwt_claims_follow_githubs_rules():
    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(
            token, TEST_APP_KEY.public_key(), algorithms=["RS256"], issuer="Iv1.client"
        )
        assert claims["iss"] == "Iv1.client"  # the client id, as GitHub recommends
        assert claims["iat"] < time.time()  # backdated for clock drift
        assert claims["exp"] - claims["iat"] <= 10 * 60 + 60  # never beyond GitHub's cap
        assert claims["exp"] - time.time() <= 10 * 60
        return httpx.Response(201, json={"token": "ghs_installation"})

    assert make_client(handler).installation_token(99) == "ghs_installation"


def test_installation_token_endpoint_and_headers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"] = request.method, request.url.path
        seen["version"] = request.headers["X-GitHub-Api-Version"]
        return httpx.Response(201, json={"token": "t"})

    make_client(handler).installation_token(12345)
    assert seen == {
        "method": "POST",
        "path": "/app/installations/12345/access_tokens",
        "version": "2022-11-28",
    }


def test_removed_installation_is_a_user_facing_access_error():
    with pytest.raises(RepositoryAccessError, match="Reconnect"):
        make_client(lambda r: httpx.Response(404)).installation_token(1)


@pytest.mark.parametrize("status", [401, 403, 500])
def test_other_failures_are_github_errors(status):
    with pytest.raises(GitHubError):
        make_client(lambda r: httpx.Response(status)).installation_token(1)


def test_malformed_token_response_is_a_github_error():
    with pytest.raises(GitHubError):
        make_client(lambda r: httpx.Response(201, json={"nope": 1})).installation_token(1)


def test_network_failure_is_a_github_error():
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(GitHubError):
        make_client(handler).installation_token(1)


def repo(n: int) -> dict:
    return {"full_name": f"octo/r{n}", "private": n % 2 == 0}


def listing_handler(pages):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "ghs_inst"})
        assert request.headers["Authorization"] == "Bearer ghs_inst"  # not the app JWT
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"repositories": pages[page - 1]})

    return handler


def test_list_repositories_uses_the_installation_token_and_paginates():
    pages = [[repo(n) for n in range(100)], [repo(100)]]
    repos = make_client(listing_handler(pages)).list_repositories(5)
    assert len(repos) == 101
    assert repos[0].full_name == "octo/r0" and repos[0].private is True
    assert repos[1].private is False


def test_list_repositories_rejects_malformed_responses():
    with pytest.raises(GitHubError):
        make_client(listing_handler([[{"full_name": "octo/r"}]])).list_repositories(5)
