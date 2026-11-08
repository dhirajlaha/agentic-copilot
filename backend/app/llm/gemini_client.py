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
    LLMOutputError,
    LLMRequestError,
    LLMResult,
    LLMUnavailableError,
    LLMUsage,
    T,
)

# 408 request timeout and 429 rate limit are transient; every other 4xx is the caller's fault.
_TRANSIENT_CLIENT_CODES = frozenset({408, 429})


class GeminiLLMClient:
    def __init__(
        self, client: genai.Client, *, provider: str, model: str, max_output_tokens: int
    ) -> None:
        self._client = client
        self.provider = provider
        self.model = model
        self._max_output_tokens = max_output_tokens

    def generate_structured(self, *, system: str, prompt: str, schema: type[T]) -> LLMResult[T]:
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
                ),
            )
        except errors.ServerError as exc:
            raise LLMUnavailableError(f"{self.provider}: server error {exc.code}") from exc
        except errors.ClientError as exc:
            if exc.code in _TRANSIENT_CLIENT_CODES:
                raise LLMUnavailableError(f"{self.provider}: busy or rate limited") from exc
            raise LLMRequestError(f"{self.provider}: request rejected ({exc.code})") from exc
        except errors.APIError as exc:
            raise LLMRequestError(f"{self.provider}: request failed ({exc.code})") from exc
        except httpx.HTTPError as exc:  # timeouts and connection failures
            raise LLMUnavailableError(f"{self.provider}: connection failed: {exc}") from exc
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
