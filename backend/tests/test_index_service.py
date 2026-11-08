from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.exceptions import IndexBusyError, NotFoundError, RepositoryAccessError
from app.db.enums import IndexStatus
from app.db.models import CodeChunk, RepoConnection
from app.llm.client import LLMUnavailableError
from app.services.index_service import RepoIndexer
from tests.factories import create_repo, create_user
from tests.fakes import FakeEmbedder, FakeSnapshots

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
SMALL = "def f():\n    return 1\n"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def repo(session):
    user = create_user(session)
    repo = create_repo(session, user, "octo/demo")
    session.commit()
    return repo


def make(session_factory, snapshots=None, embedder=None, clock=None, **kwargs):
    snapshots = snapshots or FakeSnapshots({"a.py": SMALL, "b.py": SMALL + "\n# b\n"})
    embedder = embedder or FakeEmbedder()
    indexer = RepoIndexer(
        session_factory=session_factory,
        snapshots=snapshots,
        embedder=embedder,
        clock=clock or (lambda: NOW),
        sleep=kwargs.pop("sleep", lambda seconds: None),
        **kwargs,
    )
    return indexer, snapshots, embedder


def sync(indexer, repo, heartbeat=lambda: None):
    return indexer.sync(
        repo_connection_id=repo.id, repo_full_name="octo/demo", token="tok", heartbeat=heartbeat
    )


def state(session, repo) -> RepoConnection:
    session.expire_all()
    return session.get(RepoConnection, repo.id)


def chunk_count(session, repo) -> int:
    return session.scalar(
        select(func.count()).select_from(CodeChunk).where(CodeChunk.repo_connection_id == repo.id)
    )


# ---- first index --------------------------------------------------------------------


def test_first_sync_indexes_every_file_and_records_the_commit(session, session_factory, repo):
    indexer, snapshots, _ = make(session_factory)
    beats = []

    result = sync(indexer, repo, heartbeat=lambda: beats.append(1))

    assert not result.up_to_date
    assert (result.files_total, result.files_indexed, result.files_unchanged) == (2, 2, 0)
    assert result.commit_sha == snapshots.commit
    assert result.chunks_written == chunk_count(session, repo) == 2
    after = state(session, repo)
    assert after.index_status == IndexStatus.READY
    assert after.indexed_commit_sha == snapshots.commit and after.indexed_at == NOW
    assert after.index_error is None and after.index_lease_expires_at is None
    assert beats  # the caller's lease was kept alive


def test_what_is_embedded_includes_the_path_and_line_range(session, session_factory, repo):
    indexer, _, embedder = make(session_factory, FakeSnapshots({"src/x.py": SMALL}))
    sync(indexer, repo)
    (text,) = embedder.batches[0]
    assert text.startswith("src/x.py (lines 1-2)\n") and "return 1" in text


def test_chunks_store_line_numbers_and_the_file_blob_sha(session, session_factory, repo):
    long_file = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(200))
    indexer, _, _ = make(session_factory, FakeSnapshots({"big.py": long_file}))
    sync(indexer, repo)
    rows = session.scalars(select(CodeChunk).order_by(CodeChunk.chunk_index)).all()
    assert len(rows) > 1 and rows[0].start_line == 1
    assert len({r.blob_sha for r in rows}) == 1 and len(rows[0].blob_sha) == 40
    assert [r.chunk_index for r in rows] == list(range(len(rows)))
    assert all(len(r.embedding) == 768 for r in rows)


def test_progress_is_saved_in_batches(session, session_factory, repo):
    files = {f"f{i}.py": SMALL for i in range(7)}
    indexer, _, embedder = make(session_factory, FakeSnapshots(files), FakeEmbedder(batch_size=3))
    sync(indexer, repo)
    assert [len(b) for b in embedder.batches] == [3, 3, 1]
    assert chunk_count(session, repo) == 7


# ---- staying in sync ----------------------------------------------------------------


def test_an_unchanged_head_commit_does_no_work(session, session_factory, repo):
    indexer, snapshots, embedder = make(session_factory)
    sync(indexer, repo)
    embedded, downloads = embedder.texts_embedded, snapshots.snapshot_calls

    result = sync(indexer, repo)

    assert result.up_to_date
    assert embedder.texts_embedded == embedded
    assert snapshots.snapshot_calls == downloads  # nothing downloaded either


def test_only_changed_files_are_re_embedded_and_removed_files_disappear(
    session, session_factory, repo
):
    snapshots = FakeSnapshots({"keep.py": SMALL, "edit.py": SMALL, "gone.py": SMALL})
    indexer, _, embedder = make(session_factory, snapshots)
    sync(indexer, repo)
    keep_ids = set(
        session.scalars(select(CodeChunk.id).where(CodeChunk.file_path == "keep.py")).all()
    )
    embedder.batches.clear()

    snapshots.commit = "b" * 40
    snapshots.files["edit.py"] = "def f():\n    return 'edited'\n"
    snapshots.files["new.py"] = SMALL
    del snapshots.files["gone.py"]
    result = sync(indexer, repo)

    assert (result.files_indexed, result.files_unchanged, result.files_deleted) == (2, 1, 1)
    assert sorted(t.split(" ")[0] for b in embedder.batches for t in b) == ["edit.py", "new.py"]
    session.expire_all()
    paths = set(session.scalars(select(CodeChunk.file_path)).all())
    assert paths == {"keep.py", "edit.py", "new.py"}
    # Untouched files keep their rows (same ids), not delete-and-recreate.
    assert keep_ids == set(
        session.scalars(select(CodeChunk.id).where(CodeChunk.file_path == "keep.py")).all()
    )
    assert state(session, repo).indexed_commit_sha == "b" * 40


def test_a_new_commit_with_identical_content_costs_no_embeddings(session, session_factory, repo):
    indexer, snapshots, embedder = make(session_factory)
    sync(indexer, repo)
    embedded = embedder.texts_embedded
    snapshots.commit = "d" * 40  # e.g. a docs-only commit that touches unindexed files

    result = sync(indexer, repo)

    assert not result.up_to_date and result.files_indexed == 0
    assert embedder.texts_embedded == embedded
    assert state(session, repo).indexed_commit_sha == "d" * 40


def test_skipped_files_and_secret_paths_are_reported(session, session_factory, repo):
    snapshots = FakeSnapshots({"a.py": SMALL})
    snapshots.skipped = {"secret_file": 1, "binary": 2}
    snapshots.secret_paths = [".env"]
    indexer, _, embedder = make(session_factory, snapshots)

    result = sync(indexer, repo)

    assert result.skipped == {"secret_file": 1, "binary": 2}
    assert result.secret_paths == [".env"]
    assert all(".env" not in t for b in embedder.batches for t in b)


# ---- failure and resume -------------------------------------------------------------


def test_a_failed_sync_keeps_progress_and_the_next_one_resumes(session, session_factory, repo):
    files = {f"f{i}.py": SMALL + f"# {i}\n" for i in range(6)}
    snapshots = FakeSnapshots(files)

    class FlakyEmbedder(FakeEmbedder):
        def embed_documents(self, texts):
            if len(self.batches) == 1:  # second batch fails
                raise LLMUnavailableError("gemini: busy or rate limited")
            return super().embed_documents(texts)

    flaky = FlakyEmbedder(batch_size=2)
    indexer, _, _ = make(session_factory, snapshots, flaky)

    with pytest.raises(LLMUnavailableError):
        sync(indexer, repo)

    after = state(session, repo)
    assert after.index_status == IndexStatus.FAILED
    assert after.index_error == "gemini: busy or rate limited"
    assert after.indexed_commit_sha is None and after.index_lease_expires_at is None
    assert chunk_count(session, repo) == 2  # the first batch was kept

    healthy = FakeEmbedder(batch_size=2)
    resumed, _, _ = make(session_factory, snapshots, healthy)
    result = sync(resumed, repo)

    assert healthy.texts_embedded == 4  # only the four files not yet saved
    assert result.files_indexed == 4 and result.files_unchanged == 2
    assert chunk_count(session, repo) == 6
    ok = state(session, repo)
    assert ok.index_status == IndexStatus.READY and ok.index_error is None


def test_unexpected_errors_are_not_leaked_into_the_stored_error(session, session_factory, repo):
    embedder = FakeEmbedder()
    embedder.error = RuntimeError("db password=hunter2")
    indexer, _, _ = make(session_factory, embedder=embedder)

    with pytest.raises(RuntimeError):
        sync(indexer, repo)

    error = state(session, repo).index_error
    assert error == "Indexing failed unexpectedly." and "hunter2" not in error


def test_repository_errors_are_stored_with_their_message(session, session_factory, repo):
    class Broken(FakeSnapshots):
        def snapshot(self, *args):
            raise RepositoryAccessError("octo/demo is too large to index")

    indexer, _, _ = make(session_factory, Broken({"a.py": SMALL}))
    with pytest.raises(RepositoryAccessError):
        sync(indexer, repo)
    assert state(session, repo).index_error == "octo/demo is too large to index"


def test_an_unknown_repository_is_not_found(session_factory):
    import uuid

    indexer, _, _ = make(session_factory)
    with pytest.raises(NotFoundError):
        indexer.sync(
            repo_connection_id=uuid.uuid4(),
            repo_full_name="octo/demo",
            token="t",
            heartbeat=lambda: None,
        )


# ---- two workers ---------------------------------------------------------------------


def hold_lease(session, repo, until):
    repo = session.get(RepoConnection, repo.id)
    repo.index_status = IndexStatus.INDEXING
    repo.index_lease_expires_at = until
    session.commit()


def test_a_live_lease_blocks_then_times_out(session, session_factory, repo):
    clock = Clock()
    hold_lease(session, repo, NOW + timedelta(hours=1))
    beats = []

    def sleep(seconds):
        clock.now += timedelta(seconds=seconds)

    indexer, _, embedder = make(
        session_factory, clock=clock, sleep=sleep, wait_seconds=30, poll_seconds=10
    )
    with pytest.raises(IndexBusyError):
        sync(indexer, repo, heartbeat=lambda: beats.append(1))

    assert embedder.texts_embedded == 0
    assert len(beats) == 3  # the run's own lease was kept alive while it waited
    assert state(session, repo).index_status == IndexStatus.INDEXING  # not ours to touch


def test_waiting_ends_early_when_the_other_worker_finishes(session, session_factory, repo):
    hold_lease(session, repo, NOW + timedelta(hours=1))
    snapshots = FakeSnapshots({"a.py": SMALL})

    def sleep(seconds):  # the other worker completes during our first wait
        row = session.get(RepoConnection, repo.id)
        row.index_status = IndexStatus.READY
        row.indexed_commit_sha = snapshots.commit
        session.commit()

    indexer, _, embedder = make(session_factory, snapshots, sleep=sleep)
    result = sync(indexer, repo)

    assert result.up_to_date and embedder.texts_embedded == 0


def test_an_expired_lease_is_taken_over(session, session_factory, repo):
    hold_lease(session, repo, NOW - timedelta(seconds=1))  # that worker died
    indexer, _, _ = make(session_factory)
    result = sync(indexer, repo)
    assert result.files_indexed == 2
    assert state(session, repo).index_status == IndexStatus.READY


def test_only_one_claim_wins(session, session_factory, repo):
    from app.repositories.repo_connection_repository import RepoConnectionRepository

    repos = RepoConnectionRepository(session)
    assert repos.claim_index(repo.id, NOW, NOW + timedelta(minutes=5)) is True
    assert repos.claim_index(repo.id, NOW, NOW + timedelta(minutes=5)) is False
    assert repos.claim_index(repo.id, NOW + timedelta(minutes=6), NOW + timedelta(hours=1)) is True
