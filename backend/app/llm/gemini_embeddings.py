"""Gemini text embeddings through `google-genai` (`gemini-embedding-001`).

`gemini-embedding-2` is deliberately not used for batches: it is multimodal and merges
several inputs into ONE vector, which would silently misalign chunks and embeddings.
`gemini-embedding-001` returns one vector per input.

Rate limits are a normal part of indexing a repository (the free tier allows 30,000
embedding tokens per minute), so this adapter does two things about them. It keeps each
request under a token budget, so one request can never exceed the quota by itself. And when
Google answers 429 with "retry in N seconds" it waits that long and retries, instead of
retrying blindly and giving up after a few seconds.
"""

import logging
import math
import re
import time
from collections.abc import Callable, Sequence

import httpx
from google import genai
from google.genai import errors, types

from app.llm.client import LLMOutputError
from app.llm.gemini_client import translate_error

logger = logging.getLogger(__name__)

# Source code averages about 3 characters per token; assuming fewer makes the estimate
# err on the side of smaller (safer) requests.
CHARS_PER_TOKEN = 3
DEFAULT_RETRY_SECONDS = 30.0
MAX_RETRY_SECONDS = 90.0
_RETRY_IN_MESSAGE = re.compile(r"retry in ([0-9.]+)s", re.IGNORECASE)
_RETRY_DELAY_VALUE = re.compile(r"^([0-9.]+)s$")


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN + 1


def retry_delay_seconds(exc: errors.APIError) -> float:
    """How long Google asks us to wait, from its RetryInfo detail or its message."""
    details = exc.details if isinstance(exc.details, dict) else {}
    for item in (details.get("error") or {}).get("details") or []:
        match = _RETRY_DELAY_VALUE.match(str((item or {}).get("retryDelay", "")))
        if match:
            return float(match.group(1))
    message = _RETRY_IN_MESSAGE.search(str(getattr(exc, "message", "") or ""))
    return float(message.group(1)) if message else DEFAULT_RETRY_SECONDS


class GeminiEmbeddingClient:
    def __init__(
        self,
        client: genai.Client,
        *,
        provider: str,
        model: str,
        dimensions: int,
        batch_size: int,
        max_batch_tokens: int = 20_000,
        rate_limit_retries: int = 6,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self.provider = provider
        self.model = model
        self.dimensions = dimensions
        self.batch_size = batch_size
        self._max_batch_tokens = max_batch_tokens
        self._rate_limit_retries = rate_limit_retries
        self._sleep = sleep

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for batch in self._batches(texts):
            vectors += self._embed(batch, "RETRIEVAL_DOCUMENT")
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "CODE_RETRIEVAL_QUERY")[0]

    def _batches(self, texts: Sequence[str]) -> list[list[str]]:
        """Group texts so no request exceeds the count or the estimated token budget."""
        batches: list[list[str]] = []
        current: list[str] = []
        tokens = 0
        for text in texts:
            cost = estimate_tokens(text)
            if current and (
                len(current) >= self.batch_size or tokens + cost > self._max_batch_tokens
            ):
                batches.append(current)
                current, tokens = [], 0
            current.append(text)
            tokens += cost
        if current:
            batches.append(current)
        return batches

    def _embed(self, batch: Sequence[str], task_type: str) -> list[list[float]]:
        if any(not text.strip() for text in batch):
            raise ValueError("Cannot embed an empty text")
        response = self._request(batch, task_type)
        embeddings = response.embeddings or []
        if len(embeddings) != len(batch):
            # A shifted result would attach the wrong vector to a chunk: never accept it.
            raise LLMOutputError(
                f"{self.provider}: returned {len(embeddings)} embeddings for {len(batch)} texts"
            )
        vectors = [list(e.values or []) for e in embeddings]
        for vector in vectors:
            if len(vector) != self.dimensions or not all(math.isfinite(x) for x in vector):
                raise LLMOutputError(f"{self.provider}: unusable embedding returned")
        return vectors

    def _request(self, batch: Sequence[str], task_type: str) -> types.EmbedContentResponse:
        for attempt in range(self._rate_limit_retries + 1):
            try:
                return self._client.models.embed_content(
                    model=self.model,
                    contents=list(batch),
                    config=types.EmbedContentConfig(
                        task_type=task_type, output_dimensionality=self.dimensions
                    ),
                )
            except errors.ClientError as exc:
                if exc.code == 429 and attempt < self._rate_limit_retries:
                    delay = min(retry_delay_seconds(exc) + 1.0, MAX_RETRY_SECONDS)
                    logger.info("embedding rate limit reached, waiting %.0fs", delay)
                    self._sleep(delay)
                    continue
                raise translate_error(exc, self.provider) from exc
            except (errors.APIError, httpx.HTTPError) as exc:
                raise translate_error(exc, self.provider) from exc
        raise AssertionError("unreachable")  # pragma: no cover
