"""Any OpenAI-compatible Chat Completions endpoint, through the official `openai` SDK.

Covers OpenAI itself plus anything that speaks the same protocol, selected by base URL:
Azure OpenAI (v1 endpoint) and a self-hosted vLLM server (week 8). Claude is deliberately
NOT routed through here (it uses the Anthropic SDK, `anthropic_client.py`), and neither is
Gemini (it uses Google's SDK, `gemini_client.py`).
"""

import time

import openai
from pydantic import ValidationError

from app.llm.client import (
    LLMOutputError,
    LLMRequestError,
    LLMResult,
    LLMUnavailableError,
    LLMUsage,
    T,
)


class OpenAICompatibleLLMClient:
    def __init__(
        self, client: openai.OpenAI, *, provider: str, model: str, max_output_tokens: int
    ) -> None:
        self._client = client
        self.provider = provider
        self.model = model
        self._max_output_tokens = max_output_tokens

    def generate_structured(self, *, system: str, prompt: str, schema: type[T]) -> LLMResult[T]:
        started = time.perf_counter()
        try:
            # `parse` sends a strict JSON schema as `response_format` and validates the
            # reply into the Pydantic model.
            completion = self._client.chat.completions.parse(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                response_format=schema,
                max_completion_tokens=self._max_output_tokens,
            )
        except openai.LengthFinishReasonError as exc:
            raise LLMOutputError(f"{self.provider}: output truncated at max tokens") from exc
        except openai.ContentFilterFinishReasonError as exc:
            raise LLMOutputError(f"{self.provider}: blocked by content filter") from exc
        except openai.APIConnectionError as exc:  # includes APITimeoutError
            raise LLMUnavailableError(f"{self.provider}: connection failed: {exc}") from exc
        except openai.RateLimitError as exc:
            raise LLMUnavailableError(f"{self.provider}: rate limited") from exc
        except openai.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMUnavailableError(
                    f"{self.provider}: server error {exc.status_code}"
                ) from exc
            raise LLMRequestError(f"{self.provider}: request rejected ({exc.status_code})") from exc
        except (ValidationError, ValueError) as exc:
            raise LLMOutputError(f"{self.provider}: output did not match the schema") from exc
        latency_ms = round((time.perf_counter() - started) * 1000)

        message = completion.choices[0].message
        if message.refusal:
            raise LLMOutputError(f"{self.provider}: model declined")
        if message.parsed is None:
            raise LLMOutputError(f"{self.provider}: no structured output returned")

        usage = completion.usage
        return LLMResult(
            output=message.parsed,
            usage=LLMUsage(
                usage.prompt_tokens if usage else None,
                usage.completion_tokens if usage else None,
            ),
            model=completion.model,
            latency_ms=latency_ms,
            request_id=getattr(completion, "_request_id", None),
        )
