import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UuidPk
from app.db.models.repo_connection import RepoConnection

EMBEDDING_DIM = 384  # bge-small-en-v1.5


class CodeChunk(Base):
    __tablename__ = "code_chunks"

    id: Mapped[UuidPk]
    # Every retrieval query MUST filter on this, so tenants' code never mixes.
    repo_connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repo_connections.id", ondelete="CASCADE"), index=True
    )
    file_path: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    repo_connection: Mapped[RepoConnection] = relationship(back_populates="code_chunks")
