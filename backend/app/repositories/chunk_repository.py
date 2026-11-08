from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import CodeChunk


@dataclass(frozen=True, slots=True)
class ChunkToStore:
    chunk_index: int
    start_line: int
    end_line: int
    content: str
    embedding: list[float]


@dataclass(frozen=True, slots=True)
class ChunkHit:
    file_path: str
    start_line: int
    end_line: int
    content: str
    distance: float  # cosine distance: 0 is identical, larger is less similar


class ChunkRepository:
    """Every method takes `repo_connection_id`: one tenant's code must never be visible
    through another's queries."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def indexed_files(self, repo_connection_id: UUID) -> dict[str, str]:
        """file path -> blob SHA it was indexed at."""
        rows = self._session.execute(
            select(CodeChunk.file_path, CodeChunk.blob_sha)
            .where(CodeChunk.repo_connection_id == repo_connection_id)
            .distinct()
        )
        return dict(rows.all())

    def replace_file(
        self,
        repo_connection_id: UUID,
        file_path: str,
        blob_sha: str,
        chunks: Sequence[ChunkToStore],
    ) -> None:
        """Swap a file's chunks for new ones in one transaction, so a reader never sees
        a half-updated file."""
        self._session.execute(
            delete(CodeChunk).where(
                CodeChunk.repo_connection_id == repo_connection_id,
                CodeChunk.file_path == file_path,
            )
        )
        self._session.add_all(
            CodeChunk(
                repo_connection_id=repo_connection_id,
                file_path=file_path,
                blob_sha=blob_sha,
                chunk_index=c.chunk_index,
                start_line=c.start_line,
                end_line=c.end_line,
                content=c.content,
                embedding=c.embedding,
            )
            for c in chunks
        )
        self._session.flush()

    def delete_files(self, repo_connection_id: UUID, file_paths: Sequence[str]) -> None:
        if file_paths:
            self._session.execute(
                delete(CodeChunk).where(
                    CodeChunk.repo_connection_id == repo_connection_id,
                    CodeChunk.file_path.in_(file_paths),
                )
            )

    def search(
        self, repo_connection_id: UUID, embedding: list[float], limit: int
    ) -> list[ChunkHit]:
        """The `limit` chunks closest to `embedding` within one repository."""
        distance = CodeChunk.embedding.cosine_distance(embedding)
        rows = self._session.execute(
            select(CodeChunk, distance.label("distance"))
            .where(CodeChunk.repo_connection_id == repo_connection_id)
            .order_by(distance)
            .limit(limit)
        )
        return [
            ChunkHit(c.file_path, c.start_line, c.end_line, c.content, float(d)) for c, d in rows
        ]
