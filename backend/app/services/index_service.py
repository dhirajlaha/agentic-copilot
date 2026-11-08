"""Keeps a repository's code index in sync with its default branch.

Called at the start of every run, so agents always read an index that matches the code
they are about to change. In the common case (nothing pushed since last time) it costs two
GitHub requests and no model calls. When files changed, only those files are re-embedded,
identified by git blob SHA. Progress is saved in batches, so a run that fails halfway
resumes where it stopped instead of starting over.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.exceptions import AppError, IndexBusyError, NotFoundError
from app.db.enums import IndexStatus
from app.indexing.chunker import Chunk, chunk_text
from app.indexing.snapshot import SnapshotFile, SnapshotProvider
from app.llm.client import LLMError
from app.llm.embedding_client import EmbeddingClient
from app.repositories.chunk_repository import ChunkRepository, ChunkToStore
from app.repositories.repo_connection_repository import RepoConnectionRepository

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class IndexResult:
    commit_sha: str
    up_to_date: bool  # the index already matched the head commit: nothing was fetched
    files_total: int = 0
    files_indexed: int = 0
    files_unchanged: int = 0
    files_deleted: int = 0
    chunks_written: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    secret_paths: list[str] = field(default_factory=list)


class RepoIndexer:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        snapshots: SnapshotProvider,
        embedder: EmbeddingClient,
        clock: Callable[[], datetime] = _now,
        sleep: Callable[[float], None] = time.sleep,
        lease_seconds: int = 900,
        wait_seconds: int = 900,
        poll_seconds: float = 5.0,
    ) -> None:
        self._session_factory = session_factory
        self._snapshots = snapshots
        self._embedder = embedder
        self._clock = clock
        self._sleep = sleep
        self._lease = timedelta(seconds=lease_seconds)
        self._wait = timedelta(seconds=wait_seconds)
        self._poll = poll_seconds

    @property
    def embedding_model(self) -> str:
        return self._embedder.model

    def sync(
        self,
        *,
        repo_connection_id: UUID,
        repo_full_name: str,
        token: str,
        heartbeat: Callable[[], None],
    ) -> IndexResult:
        """Bring the index up to the default branch's head commit.

        `heartbeat` is called regularly so the caller can keep its own lease alive during
        long indexing. Raises `IndexBusyError` if another worker's indexing outlasts the
        wait, and an `AppError`/`LLMError` for anything the user should be told about.
        """
        head = self._snapshots.head_commit(repo_full_name, token)
        if self._acquire(repo_connection_id, head, heartbeat):
            return IndexResult(commit_sha=head, up_to_date=True)
        try:
            return self._index(repo_connection_id, repo_full_name, token, head, heartbeat)
        except Exception as exc:
            message = str(exc) if isinstance(exc, AppError | LLMError) else None
            self._mark_failed(repo_connection_id, message or "Indexing failed unexpectedly.")
            raise

    # -- locking -----------------------------------------------------------------------

    def _acquire(self, repo_id: UUID, head: str, heartbeat: Callable[[], None]) -> bool:
        """Claim the repository for indexing. Returns True instead when another worker
        finished the job while we waited (the index is already at `head`)."""
        deadline = self._clock() + self._wait
        while True:
            with self._session_factory() as session:
                repos = RepoConnectionRepository(session)
                repo = repos.get(repo_id)
                if repo is None:
                    raise NotFoundError("Repository")
                if repo.index_status == IndexStatus.READY and repo.indexed_commit_sha == head:
                    return True
                now = self._clock()
                if repos.claim_index(repo_id, now, now + self._lease):
                    session.commit()
                    return False
                session.rollback()
            if self._clock() >= deadline:
                raise IndexBusyError(
                    "Another run is still indexing this repository. Try again in a few minutes."
                )
            heartbeat()
            self._sleep(self._poll)

    def _mark_failed(self, repo_id: UUID, message: str) -> None:
        with self._session_factory() as session:
            RepoConnectionRepository(session).finish_index(
                repo_id, status=IndexStatus.FAILED, now=self._clock(), error=message
            )
            session.commit()

    # -- the work ----------------------------------------------------------------------

    def _index(
        self,
        repo_id: UUID,
        repo_full_name: str,
        token: str,
        head: str,
        heartbeat: Callable[[], None],
    ) -> IndexResult:
        snapshot = self._snapshots.snapshot(repo_full_name, token, head)
        heartbeat()
        with self._session_factory() as session:
            existing = ChunkRepository(session).indexed_files(repo_id)

        changed = sorted(p for p, f in snapshot.files.items() if existing.get(p) != f.sha)
        deleted = sorted(set(existing) - set(snapshot.files))

        written = 0
        pending: list[tuple[SnapshotFile, list[Chunk]]] = []
        pending_chunks = 0
        for path in changed:
            file = snapshot.files[path]
            chunks = chunk_text(file.text)
            pending.append((file, chunks))
            pending_chunks += len(chunks)
            if pending_chunks >= self._embedder.batch_size:
                written += self._flush(repo_id, pending, heartbeat)
                pending, pending_chunks = [], 0
        if pending:
            written += self._flush(repo_id, pending, heartbeat)

        with self._session_factory() as session:
            repos = RepoConnectionRepository(session)
            ChunkRepository(session).delete_files(repo_id, deleted)
            repos.finish_index(
                repo_id, status=IndexStatus.READY, now=self._clock(), commit_sha=head
            )
            session.commit()

        logger.info(
            "index synced",
            extra={
                "repo_connection_id": str(repo_id),
                "files_indexed": len(changed),
                "chunks": written,
                "files_deleted": len(deleted),
            },
        )
        return IndexResult(
            commit_sha=head,
            up_to_date=False,
            files_total=len(snapshot.files),
            files_indexed=len(changed),
            files_unchanged=len(snapshot.files) - len(changed),
            files_deleted=len(deleted),
            chunks_written=written,
            skipped=dict(snapshot.skipped),
            secret_paths=sorted(snapshot.secret_paths),
        )

    def _flush(
        self,
        repo_id: UUID,
        pending: list[tuple[SnapshotFile, list[Chunk]]],
        heartbeat: Callable[[], None],
    ) -> int:
        """Embed a batch of files and save them, committing so progress survives a crash."""
        # The path and line range are part of what is embedded, so a search for a file or
        # function name can match on them as well as on the code.
        texts = [
            f"{file.path} (lines {c.start_line}-{c.end_line})\n{c.content}"
            for file, chunks in pending
            for c in chunks
        ]
        vectors = iter(self._embedder.embed_documents(texts) if texts else [])
        with self._session_factory() as session:
            chunk_repo = ChunkRepository(session)
            for file, chunks in pending:
                chunk_repo.replace_file(
                    repo_id,
                    file.path,
                    file.sha,
                    [
                        ChunkToStore(c.index, c.start_line, c.end_line, c.content, next(vectors))
                        for c in chunks
                    ],
                )
            RepoConnectionRepository(session).renew_index_lease(
                repo_id, self._clock() + self._lease
            )
            session.commit()
        heartbeat()
        return len(texts)
