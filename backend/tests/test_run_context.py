import httpx
import pytest

from app.indexing.snapshot import git_blob_sha  # noqa: F401  (documenting the SHA used below)
from app.integrations.github_files import GitHubFileReader
from app.repositories.chunk_repository import ChunkRepository, ChunkToStore
from app.services.run_context import RunContextBuilder
from tests.factories import create_repo, create_user
from tests.fakes import FakeEmbedder


@pytest.fixture
def embedder():
    return FakeEmbedder()


def builder(session_factory, embedder, handler=lambda r: httpx.Response(200, content=b"x = 1\n")):
    return RunContextBuilder(
        session_factory=session_factory,
        file_reader=GitHubFileReader(httpx.Client(transport=httpx.MockTransport(handler))),
        embedder=embedder,
    )


def test_files_are_read_at_the_runs_commit_with_its_token(session, session_factory, embedder):
    repo = create_repo(session, create_user(session), "octo/demo")
    session.commit()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.params["ref"], request.headers["authorization"]))
        return httpx.Response(200, content=b"x = 1\n")

    context = builder(session_factory, embedder, handler).build(
        repo_connection_id=repo.id, repo_full_name="octo/demo", commit_sha="f" * 40, token="t0k"
    )

    assert context.files.read_text("a.py") == "x = 1\n"
    assert seen == [("f" * 40, "Bearer t0k")]


def test_a_file_is_fetched_once_even_if_asked_for_repeatedly(session, session_factory, embedder):
    repo = create_repo(session, create_user(session))
    session.commit()
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, content=b"hello\n")

    context = builder(session_factory, embedder, handler).build(
        repo_connection_id=repo.id, repo_full_name="octo/demo", commit_sha="f" * 40, token="t"
    )
    for _ in range(3):
        context.files.read_text("a.py")
    assert len(calls) == 1


def test_a_missing_file_is_remembered_too(session, session_factory, embedder):
    repo = create_repo(session, create_user(session))
    session.commit()
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    context = builder(session_factory, embedder, handler).build(
        repo_connection_id=repo.id, repo_full_name="octo/demo", commit_sha="f" * 40, token="t"
    )
    assert context.files.read_text("nope.py") is None and context.files.read_text("nope.py") is None
    assert len(calls) == 1


def test_search_returns_the_runs_repository_chunks_only(session, session_factory, embedder):
    user = create_user(session)
    mine, theirs = (
        create_repo(session, user, "octo/mine"),
        create_repo(session, create_user(session)),
    )
    for repo, path in ((mine, "mine/login.py"), (theirs, "theirs/login.py")):
        text = "def login(user, password): check password"
        ChunkRepository(session).replace_file(
            repo.id, path, "a" * 40,
            [ChunkToStore(0, 1, 3, text, embedder.embed_documents([text])[0])],
        )  # fmt: skip
    session.commit()

    context = builder(session_factory, embedder).build(
        repo_connection_id=mine.id, repo_full_name="octo/mine", commit_sha="f" * 40, token="t"
    )
    hits = context.search.search("how is the password checked at login", 5)

    assert [h.file_path for h in hits] == ["mine/login.py"]
