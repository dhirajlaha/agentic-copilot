"""Builds the per-run `RunContext`: file reading and code search bound to one repository,
commit and installation token."""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from app.agents.context import RunContext
from app.integrations.github_files import GitHubFileReader
from app.llm.embedding_client import EmbeddingClient
from app.repositories.chunk_repository import ChunkHit, ChunkRepository


class _BoundFileReader:
    """Reads files of one repository at one commit. Remembers what it has read, because
    the Coder asks again for the same file when it repairs a mistake."""

    def __init__(self, reader: GitHubFileReader, repo: str, ref: str, token: str) -> None:
        self._reader, self._repo, self._ref, self._token = reader, repo, ref, token
        self._cache: dict[str, str | None] = {}

    def read_text(self, path: str) -> str | None:
        if path not in self._cache:
            self._cache[path] = self._reader.read_text(self._repo, self._ref, path, self._token)
        return self._cache[path]


class _IndexedCodeSearch:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        embedder: EmbeddingClient,
        repo_connection_id: UUID,
    ) -> None:
        self._session_factory = session_factory
        self._embedder = embedder
        self._repo_id = repo_connection_id

    def search(self, query: str, limit: int) -> list[ChunkHit]:
        vector = self._embedder.embed_query(query)
        with self._session_factory() as session:
            return ChunkRepository(session).search(self._repo_id, vector, limit)


class RunContextBuilder:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        file_reader: GitHubFileReader,
        embedder: EmbeddingClient,
    ) -> None:
        self._session_factory = session_factory
        self._file_reader = file_reader
        self._embedder = embedder

    def build(
        self, *, repo_connection_id: UUID, repo_full_name: str, commit_sha: str, token: str
    ) -> RunContext:
        return RunContext(
            files=_BoundFileReader(self._file_reader, repo_full_name, commit_sha, token),
            search=_IndexedCodeSearch(self._session_factory, self._embedder, repo_connection_id),
        )
