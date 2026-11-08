"""What an agent may use besides the ticket, supplied per run.

The compiled agent graph is shared by every run in a process, so it must not hold anything
specific to one run (an installation token, a repository). Those travel in a `RunContext`
passed through LangGraph's per-run `configurable` settings instead.
"""

from dataclasses import dataclass
from typing import Protocol

from langchain_core.runnables import RunnableConfig

from app.repositories.chunk_repository import ChunkHit

CONTEXT_KEY = "run_context"


class FileReader(Protocol):
    def read_text(self, path: str) -> str | None:
        """The file's text at the run's base commit, or None if it does not exist.

        Raises `RepositoryAccessError` if it exists but cannot be read (too large, binary,
        GitHub unreachable).
        """
        ...


class CodeSearch(Protocol):
    def search(self, query: str, limit: int) -> list[ChunkHit]:
        """The chunks of this run's repository closest in meaning to `query`."""
        ...


@dataclass(frozen=True, slots=True)
class RunContext:
    files: FileReader
    search: CodeSearch


def run_context_from(config: RunnableConfig) -> RunContext:
    context = (config.get("configurable") or {}).get(CONTEXT_KEY)
    if not isinstance(context, RunContext):
        raise RuntimeError("This agent needs a RunContext in config['configurable']")
    return context
