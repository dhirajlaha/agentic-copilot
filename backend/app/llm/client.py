"""The provider-agnostic LLM interface every agent depends on.

Agents only ever see `LLMClient`. Which vendor sits behind it (Anthropic API, Claude on
AWS Bedrock or Azure Foundry, OpenAI, Azure OpenAI, Gemini, a self-hosted vLLM server)
is decided once, in `app.llm.factory`, from configuration. Swapping providers or
benchmarking one against another never touches agent code.
"""

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class LLMUsage:
    # None when a provider does not report usage (some OpenAI-compatible servers).
    input_tokens: int | None
    output_tokens: int | None

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class LLMResult(Generic[T]):
    output: T  # already validated against the requested schema
    usage: LLMUsage
    model: str  # the model that actually answered, as reported by the provider
    latency_ms: int
    request_id: str | None  # provider request id, for support tickets and tracing


class LLMClient(Protocol):
    provider: str
    model: str

    def generate_structured(
        self, *, system: str, prompt: str, schema: type[T], thinking: bool = False
    ) -> LLMResult[T]:
        """Ask for an answer that must match `schema` and return it validated.

        `thinking` asks the model to reason before answering. It is off by default: steps
        that only select or classify (the Planner) are faster and cheaper without it, and
        steps that write code (the Coder) turn it on. Providers without a switch for it
        (OpenAI-compatible endpoints) ignore the flag.

        Raises an `LLMError` subclass on any failure; never returns unvalidated data.
        """
        ...


class LLMError(Exception):
    """Base class. `retryable` tells callers whether trying again later can help."""

    retryable: bool = False


class LLMUnavailableError(LLMError):
    """Rate limited, overloaded, timed out or unreachable, after the SDK's own retries."""

    retryable = True


class LLMRequestError(LLMError):
    """The request itself was rejected (bad credentials, permissions, invalid request,
    unknown model). Retrying the same request will fail the same way."""


class LLMOutputError(LLMError):
    """The model answered, but not usably: it refused, hit the output limit, or returned
    something that does not match the schema."""
