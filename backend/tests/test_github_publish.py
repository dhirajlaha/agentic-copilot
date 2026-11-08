"""The pull request publisher against a fake GitHub that emulates the Git Data API."""

import json

import httpx
import pytest

from app.core.exceptions import GitHubError, RepositoryAccessError
from app.integrations.github_publish import FileWrite, GitHubPullRequestPublisher

REPO = "octo/demo"
BASE = "b" * 40
BRANCH = "copilot/fix-login-1a2b3c4d"

TREES = {
    "tree0": [
        {"path": "README.md", "type": "blob", "mode": "100644"},
        {"path": "run.sh", "type": "blob", "mode": "100755"},
        {"path": "src", "type": "tree", "mode": "040000", "sha": "tree-src"},
    ],
    "tree-src": [
        {"path": "login.py", "type": "blob", "mode": "100644"},
        {"path": "tool.sh", "type": "blob", "mode": "100755"},
        {"path": "pkg", "type": "tree", "mode": "040000", "sha": "tree-pkg"},
    ],
    "tree-pkg": [{"path": "mod.py", "type": "blob", "mode": "100644"}],
}


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.branch_exists = False
        self.existing_pulls: list[dict] = []
        self.pull_status = 201
        self.forbid = False
        self.truncated = False

    def body(self, method: str, suffix: str):
        return next(b for m, p, b in self.calls if m == method and p.endswith(suffix))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        self.calls.append((request.method, path, body))
        assert request.headers["authorization"] == "Bearer tok"
        if self.forbid and request.method == "POST":
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        route = (request.method, request.url.path)
        if route == ("GET", f"/repos/{REPO}"):
            return httpx.Response(200, json={"default_branch": "main"})
        if route == ("GET", f"/repos/{REPO}/git/commits/{BASE}"):
            return httpx.Response(200, json={"tree": {"sha": "tree0"}})
        if request.method == "GET" and request.url.path.startswith(f"/repos/{REPO}/git/trees/"):
            sha = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"tree": TREES[sha], "truncated": self.truncated})
        if route == ("POST", f"/repos/{REPO}/git/trees"):
            return httpx.Response(201, json={"sha": "tree1"})
        if route == ("POST", f"/repos/{REPO}/git/commits"):
            return httpx.Response(201, json={"sha": "commit1"})
        if route == ("POST", f"/repos/{REPO}/git/refs"):
            if self.branch_exists:
                return httpx.Response(422, json={"message": "Reference already exists"})
            return httpx.Response(201, json={"ref": body["ref"]})
        if route == ("POST", f"/repos/{REPO}/pulls"):
            if self.pull_status != 201:
                return httpx.Response(self.pull_status, json={"message": "nope"})
            return httpx.Response(
                201, json={"number": 7, "html_url": f"https://github.com/{REPO}/pull/7"}
            )
        if route == ("GET", f"/repos/{REPO}/pulls"):
            return httpx.Response(200, json=self.existing_pulls)
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)


def publish(fake: FakeGitHub, files=None):
    publisher = GitHubPullRequestPublisher(httpx.Client(transport=httpx.MockTransport(fake)))
    return publisher.publish(
        repo_full_name=REPO,
        token="tok",
        base_commit_sha=BASE,
        branch=BRANCH,
        files=files if files is not None else [FileWrite("README.md", "new readme\n")],
        commit_message="Fix login\n\nBody.",
        pr_title="Fix login",
        pr_body="The body",
    )


def test_creates_one_commit_on_a_new_branch_and_a_draft_pull_request():
    fake = FakeGitHub()
    result = publish(fake)

    assert (result.branch, result.number, result.url) == (
        BRANCH,
        7,
        "https://github.com/octo/demo/pull/7",
    )
    commit = fake.body("POST", "/git/commits")
    assert commit == {"message": "Fix login\n\nBody.", "tree": "tree1", "parents": [BASE]}
    assert fake.body("POST", "/git/refs") == {"ref": f"refs/heads/{BRANCH}", "sha": "commit1"}
    pull = fake.body("POST", "/pulls")
    assert pull == {
        "title": "Fix login",
        "head": BRANCH,
        "base": "main",  # the repository's default branch, read from GitHub
        "body": "The body",
        "draft": True,
    }


def test_the_commit_is_built_on_the_base_commits_tree():
    fake = FakeGitHub()
    publish(fake)
    tree = fake.body("POST", "/git/trees")
    assert tree["base_tree"] == "tree0"
    assert tree["tree"] == [
        {"path": "README.md", "mode": "100644", "type": "blob", "content": "new readme\n"}
    ]


def test_several_files_become_one_commit_with_deletes_and_creates():
    fake = FakeGitHub()
    publish(
        fake,
        [
            FileWrite("README.md", "edited\n"),
            FileWrite("src/new.py", "x = 1\n"),
            FileWrite("src/login.py", None),
        ],
    )
    entries = {e["path"]: e for e in fake.body("POST", "/git/trees")["tree"]}
    assert entries["README.md"]["content"] == "edited\n"
    assert (
        entries["src/new.py"]["mode"] == "100644" and entries["src/new.py"]["content"] == "x = 1\n"
    )
    assert entries["src/login.py"]["sha"] is None and "content" not in entries["src/login.py"]
    assert sum(1 for m, p, _ in fake.calls if m == "POST" and p.endswith("/git/commits")) == 1


def test_an_executable_file_keeps_its_mode_including_in_subdirectories():
    fake = FakeGitHub()
    publish(
        fake,
        [FileWrite("run.sh", "#!/bin/sh\n"), FileWrite("src/tool.sh", "#!/bin/sh\n"),
         FileWrite("src/pkg/mod.py", "y = 2\n")],
    )  # fmt: skip
    modes = {e["path"]: e["mode"] for e in fake.body("POST", "/git/trees")["tree"]}
    assert modes == {"run.sh": "100755", "src/tool.sh": "100755", "src/pkg/mod.py": "100644"}


def test_a_path_inside_a_missing_directory_defaults_to_a_regular_file():
    fake = FakeGitHub()
    publish(fake, [FileWrite("docs/guide/new.md", "hi\n")])
    assert fake.body("POST", "/git/trees")["tree"][0]["mode"] == "100644"


def test_a_directory_listing_that_is_truncated_is_refused():
    fake = FakeGitHub()
    fake.truncated = True
    with pytest.raises(GitHubError, match="too large to inspect"):
        publish(fake)


def test_an_existing_branch_with_a_pull_request_reuses_it():
    fake = FakeGitHub()
    fake.branch_exists = True
    fake.existing_pulls = [{"number": 3, "html_url": "https://github.com/octo/demo/pull/3"}]
    result = publish(fake)
    assert (result.number, result.url) == (3, "https://github.com/octo/demo/pull/3")
    assert not any(p.endswith("/pulls") and m == "POST" for m, p, _ in fake.calls)
    from urllib.parse import parse_qs, urlsplit

    lookup = next(p for m, p, _ in fake.calls if m == "GET" and "/pulls?" in p)
    query = parse_qs(urlsplit(lookup).query)
    assert query["head"] == [f"octo:{BRANCH}"] and query["state"] == ["all"]


def test_an_existing_branch_without_a_pull_request_is_never_overwritten():
    fake = FakeGitHub()
    fake.branch_exists = True
    with pytest.raises(GitHubError, match="already exists without a pull request"):
        publish(fake)
    assert not any(m == "DELETE" for m, _, _ in fake.calls)  # someone else's branch stays


def test_a_failed_pull_request_removes_the_branch_it_just_created():
    fake = FakeGitHub()
    fake.pull_status = 422
    with pytest.raises(GitHubError):
        publish(fake)
    deletes = [p for m, p, _ in fake.calls if m == "DELETE"]
    assert deletes == [f"/repos/{REPO}/git/refs/heads/{BRANCH}"]


def test_missing_write_permission_gives_an_actionable_message():
    fake = FakeGitHub()
    fake.forbid = True
    with pytest.raises(RepositoryAccessError, match="Contents and Pull requests write access"):
        publish(fake)


def test_a_missing_repository_or_commit_is_an_access_error():
    def handler(request):
        return httpx.Response(404)

    publisher = GitHubPullRequestPublisher(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RepositoryAccessError, match="could not find"):
        publisher.publish(
            repo_full_name=REPO, token="t", base_commit_sha=BASE, branch="b", files=[],
            commit_message="m", pr_title="t", pr_body="b",
        )  # fmt: skip


def test_the_rate_limit_is_reported_plainly():
    def handler(request):
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"})

    publisher = GitHubPullRequestPublisher(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RepositoryAccessError, match="rate limit"):
        publisher.publish(
            repo_full_name=REPO, token="t", base_commit_sha=BASE, branch="b", files=[],
            commit_message="m", pr_title="t", pr_body="b",
        )  # fmt: skip


def test_network_failures_are_github_errors():
    def handler(request):
        raise httpx.ConnectError("down")

    publisher = GitHubPullRequestPublisher(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(GitHubError):
        publisher.publish(
            repo_full_name=REPO, token="t", base_commit_sha=BASE, branch="b", files=[],
            commit_message="m", pr_title="t", pr_body="b",
        )  # fmt: skip


def test_an_invalid_repository_name_is_refused_before_any_request():
    def handler(request):
        raise AssertionError("must not call GitHub")

    publisher = GitHubPullRequestPublisher(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RepositoryAccessError, match="Invalid repository name"):
        publisher.publish(
            repo_full_name="../etc", token="t", base_commit_sha=BASE, branch="b", files=[],
            commit_message="m", pr_title="t", pr_body="b",
        )  # fmt: skip
