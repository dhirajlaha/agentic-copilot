import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UuidPk
from app.db.models.repo_connection import RepoConnection

# Must equal EMBEDDING_DIMENSIONS in the embedding settings; the app checks at startup.
# Changing it needs a migration and a full re-index (vectors of different sizes are not
# comparable).
EMBEDDING_DIM = 768  # gemini-embedding-001, truncated to 768 dimensions


class CodeChunk(Base):
    """A slice of one file in a connected repository, with its embedding."""

    __tablename__ = "code_chunks"
    __table_args__ = (
        UniqueConstraint("repo_connection_id", "file_path", "chunk_index"),
        Index("ix_code_chunks_repo_file", "repo_connection_id", "file_path"),
        # Approximate nearest-neighbour search by cosine distance.
        Index(
            "ix_code_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[UuidPk]
    # Every retrieval query MUST filter on this, so tenants' code never mixes.
    repo_connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repo_connections.id", ondelete="CASCADE"), index=True
    )
    file_path: Mapped[str] = mapped_column(Text)
    # Git blob SHA of the whole file when it was indexed. Unchanged SHA = nothing to redo.
    blob_sha: Mapped[str] = mapped_column(String(40))
    chunk_index: Mapped[int] = mapped_column(Integer)
    start_line: Mapped[int] = mapped_column(Integer)  # 1-based, inclusive
    end_line: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    repo_connection: Mapped[RepoConnection] = relationship(back_populates="code_chunks")
