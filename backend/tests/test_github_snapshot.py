"""The tarball snapshot reader: a fake GitHub serves the API calls and the archive."""

import io
import tarfile

import httpx
import pytest

from app.core.exceptions import GitHubError, RepositoryAccessError
from app.indexing.snapshot import git_blob_sha
from app.integrations import github_snapshot
from app.integrations.github_snapshot import GitHubSnapshotReader

SHA = "c" * 40
REPO = "octo/demo"


def make_tarball(files: dict[str, bytes], root: str = "octo-demo-ccccccc") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, data in files.items():
            info = tarfile.TarInfo(f"{root}/{path}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeGitHub:
    def __init__(self, tarball: bytes, branch: str = "main") -> None:
        self.tarball = tarball
        self.branch = branch
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "codeload.github.com":
            return httpx.Response(200, content=self.tarball)
        if path == f"/repos/{REPO}":
            return httpx.Response(200, json={"default_branch": self.branch})
        if path == f"/repos/{REPO}/commits/{self.branch}":
            return httpx.Response(200, text=SHA)
        if path == f"/repos/{REPO}/tarball/{SHA}":
            return httpx.Response(302, headers={"location": "https://codeload.github.com/x?sig=1"})
        return httpx.Response(404)


def reader(fake) -> GitHubSnapshotReader:
    return GitHubSnapshotReader(httpx.Client(transport=httpx.MockTransport(fake)))


def test_head_commit_reads_the_default_branch():
    fake = FakeGitHub(b"")
    assert reader(fake).head_commit(REPO, "tok") == SHA
    sha_request = fake.requests[-1]
    assert sha_request.headers["accept"] == "application/vnd.github.sha"
    assert sha_request.headers["authorization"] == "Bearer tok"


def test_snapshot_keeps_text_files_and_computes_git_blob_shas():
    code = b"def main():\n    pass\n"
    fake = FakeGitHub(make_tarball({"src/main.py": code, "README.md": b"# demo\n"}))

    snapshot = reader(fake).snapshot(REPO, "tok", SHA)

    assert snapshot.commit_sha == SHA
    assert set(snapshot.files) == {"src/main.py", "README.md"}  # archive root folder stripped
    file = snapshot.files["src/main.py"]
    assert file.text == code.decode() and file.sha == git_blob_sha(code)


def test_the_git_blob_sha_matches_git():
    # `printf 'hello\n' | git hash-object --stdin`
    assert git_blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_the_token_is_not_sent_to_the_download_host():
    fake = FakeGitHub(make_tarball({"a.py": b"x = 1\n"}))
    reader(fake).snapshot(REPO, "tok", SHA)
    download = next(r for r in fake.requests if r.url.host == "codeload.github.com")
    assert "authorization" not in download.headers
    api = [r for r in fake.requests if r.url.host == "api.github.com"]
    assert api and all(r.headers["authorization"] == "Bearer tok" for r in api)


def test_unwanted_files_are_skipped_and_counted_without_their_content():
    files = {
        "src/app.py": b"x = 1\n",
        ".env": b"DATABASE_PASSWORD=hunter2\n",
        "keys/server.pem": b"-----BEGIN RSA PRIVATE KEY-----\nabc\n",
        "config.py": b"KEY = 'AKIAIOSFODNN7EXAMPLE'\n",  # a secret inside an ordinary file
        "node_modules/x/index.js": b"module.exports = 1\n",
        "uv.lock": b"# lock\n",
        "logo.png": b"\x89PNG\r\n\x1a\n\x00\x00",
        "data.bin": b"\x00\x01\x02" * 50,
        "empty.txt": b"",
        "blank.txt": b"   \n\n",
        "huge.txt": b"a" * (256 * 1024 + 1),
    }
    snapshot = reader(FakeGitHub(make_tarball(files))).snapshot(REPO, "tok", SHA)

    assert set(snapshot.files) == {"src/app.py"}
    assert dict(snapshot.skipped) == {
        "secret_file": 2,
        "possible_secret": 1,
        "vendored_or_generated": 1,
        "lockfile": 1,
        "binary": 2,
        "empty": 2,
        "too_large": 1,
    }
    assert sorted(snapshot.secret_paths) == [".env", "config.py", "keys/server.pem"]
    # No file content reaches the snapshot object in any form.
    blob = repr(snapshot)
    assert "hunter2" not in blob and "AKIA" not in blob


def test_unsafe_archive_paths_are_ignored():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ("root/../escape.py", "root/ok.py", "/abs.py", "root/./dot.py"):
            info = tarfile.TarInfo(name)
            info.size = 3
            archive.addfile(info, io.BytesIO(b"a=1"))
        link = tarfile.TarInfo("root/link.py")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    snapshot = reader(FakeGitHub(buffer.getvalue())).snapshot(REPO, "tok", SHA)
    # Parent-directory traversal and symlinks never make it in; nothing is written to disk
    # anyway, and every stored path is a plain relative path.
    assert "escape.py" not in snapshot.files and "link.py" not in snapshot.files
    assert "ok.py" in snapshot.files
    assert all(not p.startswith("/") and ".." not in p.split("/") for p in snapshot.files)


def test_oversized_downloads_are_refused(monkeypatch):
    monkeypatch.setattr(github_snapshot, "MAX_DOWNLOAD_BYTES", 1000)
    big = make_tarball({"a.txt": __import__("os").urandom(5000)})  # incompressible
    with pytest.raises(RepositoryAccessError, match="too large"):
        reader(FakeGitHub(big)).snapshot(REPO, "tok", SHA)


def test_too_much_text_is_refused(monkeypatch):
    monkeypatch.setattr(github_snapshot, "MAX_TEXT_BYTES", 100)
    files = {f"f{i}.txt": b"hello world\n" * 3 for i in range(10)}
    with pytest.raises(RepositoryAccessError, match="too large"):
        reader(FakeGitHub(make_tarball(files))).snapshot(REPO, "tok", SHA)


def test_a_corrupt_archive_is_a_github_error():
    with pytest.raises(GitHubError):
        reader(FakeGitHub(b"this is not a tarball")).snapshot(REPO, "tok", SHA)


def test_missing_or_inaccessible_repo_is_an_access_error():
    def handler(request):
        return httpx.Response(404)

    with pytest.raises(RepositoryAccessError, match="no longer access"):
        GitHubSnapshotReader(httpx.Client(transport=httpx.MockTransport(handler))).head_commit(
            REPO, "tok"
        )


def test_an_empty_repository_has_no_default_branch():
    fake = FakeGitHub(b"")
    fake.branch = ""
    with pytest.raises(RepositoryAccessError, match="no default branch"):
        reader(fake).head_commit(REPO, "tok")


@pytest.mark.parametrize("name", ["no-slash", "../etc", "a/b/c", "owner/rep o"])
def test_repository_names_are_validated_before_any_request(name):
    def handler(request):
        raise AssertionError("must not call GitHub")

    r = GitHubSnapshotReader(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RepositoryAccessError, match="Invalid repository name"):
        r.head_commit(name, "tok")


def test_the_rate_limit_is_reported_plainly():
    def handler(request):
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"})

    r = GitHubSnapshotReader(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RepositoryAccessError, match="rate limit"):
        r.head_commit(REPO, "tok")


def test_the_snapshot_requires_a_full_commit_sha():
    with pytest.raises(ValueError, match="40"):
        reader(FakeGitHub(b"")).snapshot(REPO, "tok", "main")
