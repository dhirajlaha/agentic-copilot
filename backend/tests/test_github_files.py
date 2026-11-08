import httpx
import pytest

from app.core.exceptions import GitHubError, RepositoryAccessError
from app.indexing import policy
from app.integrations.github_files import GitHubFileReader

REPO = "octo/demo"
SHA = "e" * 40


def reader(handler) -> GitHubFileReader:
    return GitHubFileReader(httpx.Client(transport=httpx.MockTransport(handler)))


def read(handler, path="src/app.py"):
    return reader(handler).read_text(REPO, SHA, path, "tok")


def raw(content: bytes, content_type: str = "application/vnd.github.raw; charset=utf-8"):
    return lambda request: httpx.Response(
        200, content=content, headers={"content-type": content_type}
    )


def test_reads_the_file_at_the_commit_with_the_installation_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["ref"] = request.url.params["ref"]
        seen["accept"] = request.headers["accept"]
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, content=b"x = 1\n", headers={"content-type": "text/plain"})

    assert read(handler) == "x = 1\n"
    assert seen == {
        "path": "/repos/octo/demo/contents/src/app.py",
        "ref": SHA,
        "accept": "application/vnd.github.raw+json",
        "auth": "Bearer tok",
    }


def test_paths_are_url_encoded_but_keep_their_slashes():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["raw_path"] = request.url.raw_path.decode()
        return httpx.Response(200, content=b"a")

    read(handler, "docs/my file #1.md")
    assert seen["raw_path"].startswith("/repos/octo/demo/contents/docs/my%20file%20%231.md")


def test_a_missing_file_is_none_not_an_error():
    assert read(lambda r: httpx.Response(404)) is None


def test_a_json_file_is_still_a_file():
    # GitHub serves raw content as application/vnd.github.raw, whatever the file type is.
    assert read(raw(b'{"a": 1}\n')) == '{"a": 1}\n'


def test_a_directory_is_not_a_file():
    handler = raw(b'[{"name": "a"}]', "application/json; charset=utf-8")
    with pytest.raises(RepositoryAccessError, match="not a regular file"):
        read(handler)


def test_binary_files_are_refused():
    with pytest.raises(RepositoryAccessError, match="not a text file"):
        read(raw(b"\x00\x01\x02binary"))


def test_oversized_files_are_refused():
    with pytest.raises(RepositoryAccessError, match="too large"):
        read(raw(b"a" * (policy.MAX_FILE_BYTES + 1)))


def test_githubs_own_too_large_response_is_reported():
    def handler(request):
        return httpx.Response(403, json={"errors": [{"code": "too_large"}]})

    with pytest.raises(RepositoryAccessError, match="too large"):
        read(handler)


def test_rate_limits_are_reported_plainly():
    def handler(request):
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"})

    with pytest.raises(RepositoryAccessError, match="rate limit"):
        read(handler)


@pytest.mark.parametrize("status", [401, 500, 502])
def test_other_failures_are_github_errors(status):
    with pytest.raises(GitHubError):
        read(lambda r: httpx.Response(status))


def test_network_failures_are_github_errors():
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(GitHubError):
        read(handler)


def test_invalid_repository_names_are_refused_before_any_request():
    def handler(request):
        raise AssertionError("must not call GitHub")

    with pytest.raises(RepositoryAccessError, match="Invalid repository name"):
        reader(handler).read_text("../etc", SHA, "a.py", "tok")
