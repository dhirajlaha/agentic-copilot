"""The provider-agnostic embedding interface, mirroring `LLMClient`.

Indexing and retrieval only see `EmbeddingClient`; which vendor sits behind it is decided
in `app.llm.factory`. Errors reuse the `LLMError` hierarchy so callers handle one set.
"""

from collections.abc import Sequence
from typing import Protocol


class EmbeddingClient(Protocol):
    provider: str
    model: str
    dimensions: int  # every returned vector has exactly this length
    batch_size: int  # how many texts one provider request carries

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed texts to be stored and searched. One vector per text, same order.

        Raises an `LLMError` subclass on failure; never returns a wrong-sized result.
        """
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query (some providers embed queries differently)."""
        ...
