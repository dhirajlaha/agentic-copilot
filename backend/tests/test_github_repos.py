import httpx
import pytest

from app.core.exceptions import RepositoryAccessError
from app.integrations.github_repos import GitHubRepoFiles


def provider(handler) -> GitHubRepoFiles:
    return GitHubRepoFiles(httpx.Client(transport=httpx.MockTransport(handler)))


def ok_handler(seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/repos/octo/demo":
            return httpx.Response(200, json={"default_branch": "main"})
        return httpx.Response(
            200,
            json={
                "truncated": False,
                "tree": [
                    {"path": "src", "type": "tree"},
                    {"path": "src/app.py", "type": "blob"},
                    {"path": "README.md", "type": "blob"},
                    {"path": "vendor/lib", "type": "commit"},  # submodule
                ],
            },
        )

    return handler


def test_lists_blobs_on_the_default_branch():
    seen: list = []
    files = provider(ok_handler(seen)).list_files("octo/demo")
    assert files == ["src/app.py", "README.md"]
    tree_request = seen[1]
    assert tree_request.url.path == "/repos/octo/demo/git/trees/main"
    assert tree_request.url.params["recursive"] == "1"
    assert "authorization" not in tree_request.headers  # public repo, no token


def test_token_is_sent_when_given():
    seen: list = []
    provider(ok_handler(seen)).list_files("octo/demo", token="inst-token")
    assert seen[0].headers["authorization"] == "Bearer inst-token"


@pytest.mark.parametrize(
    "name", ["octo", "octo/demo/../x", "../etc/passwd", "o c/demo", "octo/demo?x=1"]
)
def test_invalid_names_are_rejected_before_any_request(name):
    def handler(request):
        raise AssertionError("no HTTP call expected")

    with pytest.raises(RepositoryAccessError, match="Invalid repository name"):
        provider(handler).list_files(name)


def test_missing_or_private_repo():
    with pytest.raises(RepositoryAccessError, match="private"):
        provider(lambda r: httpx.Response(404)).list_files("octo/demo")


def test_truncated_tree_fails_loudly():
    def handler(request):
        if request.url.path == "/repos/octo/demo":
            return httpx.Response(200, json={"default_branch": "main"})
        return httpx.Response(200, json={"truncated": True, "tree": []})

    with pytest.raises(RepositoryAccessError, match="too large"):
        provider(handler).list_files("octo/demo")


def test_rate_limit_is_explained():
    def handler(request):
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"})

    with pytest.raises(RepositoryAccessError, match="rate limit"):
        provider(handler).list_files("octo/demo")


def test_network_failure():
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(RepositoryAccessError, match="reach GitHub"):
        provider(handler).list_files("octo/demo")
