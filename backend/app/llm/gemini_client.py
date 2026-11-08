"""Google Gemini through the official `google-genai` SDK (native Gemini API).

Structured output uses `response_json_schema` with JSON mode, and the reply is validated
into the caller's Pydantic model. Prompts are sent with `generate_content`, which is
stateless: nothing is stored server-side on Google's end by this call.
"""

import time

import httpx
from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from app.llm.client import (
    LLMError,
    LLMOutputError,
    LLMRequestError,
    LLMResult,
    LLMUnavailableError,
    LLMUsage,
    T,
)

# 408 request timeout and 429 rate limit are transient; every other 4xx is the caller's fault.
_TRANSIENT_CLIENT_CODES = frozenset({408, 429})


def translate_error(exc: Exception, provider: str) -> LLMError:
    """Map an SDK or transport exception to our vendor-neutral errors (shared by the
    generation and embedding adapters)."""
    if isinstance(exc, errors.ServerError):
        return LLMUnavailableError(f"{provider}: server error {exc.code}")
    if isinstance(exc, errors.ClientError):
        if exc.code in _TRANSIENT_CLIENT_CODES:
            return LLMUnavailableError(f"{provider}: busy or rate limited")
        return LLMRequestError(f"{provider}: request rejected ({exc.code})")
    if isinstance(exc, errors.APIError):
        return LLMRequestError(f"{provider}: request failed ({exc.code})")
    return LLMUnavailableError(f"{provider}: connection failed: {exc}")  # httpx errors


class GeminiLLMClient:
    def __init__(
        self, client: genai.Client, *, provider: str, model: str, max_output_tokens: int
    ) -> None:
        self._client = client
        self.provider = provider
        self.model = model
        self._max_output_tokens = max_output_tokens

    def generate_structured(
        self, *, system: str, prompt: str, schema: type[T], thinking: bool = False
    ) -> LLMResult[T]:
        started = time.perf_counter()
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=self._max_output_tokens,
                    response_mime_type="application/json",
                    response_json_schema=schema.model_json_schema(),
                    # We never pass tools. Turning the SDK's automatic function calling off
                    # makes that explicit, so tools added later cannot be run by the SDK
                    # behind our back.
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    # A zero budget switches thinking off; leaving it unset lets the model
                    # decide how much to think.
                    thinking_config=None if thinking else types.ThinkingConfig(thinking_budget=0),
                ),
            )
        except (errors.APIError, httpx.HTTPError) as exc:  # HTTPError: timeouts, connection
            raise translate_error(exc, self.provider) from exc
        latency_ms = round((time.perf_counter() - started) * 1000)

        feedback = response.prompt_feedback
        if feedback and feedback.block_reason:
            raise LLMOutputError(f"{self.provider}: prompt blocked ({feedback.block_reason})")
        if not response.candidates:
            raise LLMOutputError(f"{self.provider}: no answer returned")

        finish = response.candidates[0].finish_reason
        if finish == types.FinishReason.MAX_TOKENS:
            raise LLMOutputError(f"{self.provider}: output truncated at max tokens")
        if finish is not None and finish != types.FinishReason.STOP:
            raise LLMOutputError(f"{self.provider}: stopped early ({finish.name})")

        try:
            text = response.text
            if not text:
                raise LLMOutputError(f"{self.provider}: no structured output returned")
            output = schema.model_validate_json(text)
        except (ValidationError, ValueError) as exc:
            raise LLMOutputError(f"{self.provider}: output did not match the schema") from exc

        return LLMResult(
            output=output,
            usage=_usage(response.usage_metadata),
            model=response.model_version or self.model,
            latency_ms=latency_ms,
            request_id=response.response_id,
        )


def _usage(meta: types.GenerateContentResponseUsageMetadata | None) -> LLMUsage:
    if meta is None or meta.prompt_token_count is None:
        return LLMUsage(None, None)
    # Thinking tokens are billed as output but reported separately by Gemini.
    output = (meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)
    return LLMUsage(meta.prompt_token_count, output)
