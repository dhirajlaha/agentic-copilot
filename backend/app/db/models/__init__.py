"""Importing this package registers every model on `Base.metadata` (Alembic relies on it)."""

from app.db.base import Base
from app.db.models.agent import AgentRun, AgentStep, TestResult
from app.db.models.benchmark import BenchmarkRun
from app.db.models.code_chunk import EMBEDDING_DIM, CodeChunk
from app.db.models.repo_connection import RepoConnection
from app.db.models.ticket import Ticket
from app.db.models.user import User, UserSession

__all__ = [
    "EMBEDDING_DIM",
    "AgentRun",
    "AgentStep",
    "Base",
    "BenchmarkRun",
    "CodeChunk",
    "RepoConnection",
    "TestResult",
    "Ticket",
    "User",
    "UserSession",
]
