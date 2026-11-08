import pytest
from sqlalchemy import func, select

from app.db.models import CodeChunk, RepoConnection
from app.repositories.chunk_repository import ChunkRepository, ChunkToStore
from tests.factories import create_repo, create_user
from tests.fakes import FakeEmbedder

SHA = "a" * 40


@pytest.fixture
def embedder():
    return FakeEmbedder()


def store(repo_chunks, session, repo, embedder, path, text, sha=SHA):
    repo_chunks.replace_file(
        repo.id, path, sha, [ChunkToStore(0, 1, 3, text, embedder.embed_documents([text])[0])]
    )


def test_search_ranks_by_similarity(session, embedder):
    repo = create_repo(session, create_user(session))
    chunks = ChunkRepository(session)
    store(
        chunks,
        session,
        repo,
        embedder,
        "auth/login.py",
        "def login(user, password): check password",
    )
    store(
        chunks,
        session,
        repo,
        embedder,
        "video/encode.py",
        "def encode_video(frames): transcode h264",
    )
    store(
        chunks, session, repo, embedder, "docs/readme.md", "installation instructions and licence"
    )

    hits = chunks.search(repo.id, embedder.embed_query("how does login check the password"), 3)

    assert hits[0].file_path == "auth/login.py"
    assert hits[0].distance < hits[1].distance <= hits[2].distance
    assert (hits[0].start_line, hits[0].end_line) == (1, 3)
    assert "check password" in hits[0].content


def test_search_respects_the_limit(session, embedder):
    repo = create_repo(session, create_user(session))
    chunks = ChunkRepository(session)
    for i in range(5):
        store(chunks, session, repo, embedder, f"f{i}.py", f"def f{i}(): pass")
    assert len(chunks.search(repo.id, embedder.embed_query("def"), 2)) == 2


def test_search_never_crosses_repositories(session, embedder):
    user = create_user(session)
    mine, theirs = create_repo(session, user), create_repo(session, create_user(session))
    chunks = ChunkRepository(session)
    store(chunks, session, mine, embedder, "mine.py", "secret billing algorithm")
    store(chunks, session, theirs, embedder, "theirs.py", "secret billing algorithm")

    hits = chunks.search(mine.id, embedder.embed_query("secret billing algorithm"), 10)

    assert [h.file_path for h in hits] == ["mine.py"]


def test_replace_file_swaps_all_of_a_files_chunks(session, embedder):
    repo = create_repo(session, create_user(session))
    chunks = ChunkRepository(session)
    vector = embedder.embed_documents(["x"])[0]
    chunks.replace_file(
        repo.id, "a.py", SHA, [ChunkToStore(i, i + 1, i + 1, f"old {i}", vector) for i in range(3)]
    )
    chunks.replace_file(repo.id, "a.py", "b" * 40, [ChunkToStore(0, 1, 1, "new", vector)])

    rows = session.scalars(select(CodeChunk)).all()
    assert [(r.content, r.blob_sha) for r in rows] == [("new", "b" * 40)]
    assert chunks.indexed_files(repo.id) == {"a.py": "b" * 40}


def test_delete_files_removes_only_the_named_files(session, embedder):
    repo = create_repo(session, create_user(session))
    chunks = ChunkRepository(session)
    for name in ("a.py", "b.py", "c.py"):
        store(chunks, session, repo, embedder, name, "x")
    chunks.delete_files(repo.id, ["a.py", "c.py"])
    chunks.delete_files(repo.id, [])  # a no-op, not an error
    assert set(chunks.indexed_files(repo.id)) == {"b.py"}


def test_chunks_are_deleted_with_their_repository(session, embedder):
    repo = create_repo(session, create_user(session))
    store(ChunkRepository(session), session, repo, embedder, "a.py", "x")
    session.delete(session.get(RepoConnection, repo.id))
    session.flush()
    assert session.scalar(select(func.count()).select_from(CodeChunk)) == 0


def test_a_chunk_position_is_unique_within_a_file(session, embedder):
    from sqlalchemy.exc import IntegrityError

    repo = create_repo(session, create_user(session))
    vector = embedder.embed_documents(["x"])[0]
    session.add_all(
        CodeChunk(
            repo_connection_id=repo.id, file_path="a.py", blob_sha=SHA, chunk_index=0,
            start_line=1, end_line=1, content="x", embedding=vector,
        )
        for _ in range(2)
    )  # fmt: skip
    with pytest.raises(IntegrityError):
        session.flush()
