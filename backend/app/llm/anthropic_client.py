"""Claude through the official Anthropic SDK.

The same adapter serves three platforms because the SDK exposes the same
`messages.parse` surface on all of them: the Claude API (`anthropic.Anthropic`), Amazon
Bedrock (`anthropic.AnthropicBedrockMantle`) and Microsoft Foundry
(`anthropic.AnthropicFoundry`). The factory picks the client class.
"""

import logging
import time

import anthropic
from pydantic import ValidationError

from app.llm.client import (
    LLMOutputError,
    LLMRequestError,
    LLMResult,
    LLMUnavailableError,
    LLMUsage,
    T,
)

logger = logging.getLogger(__name__)

AnthropicSDKClient = (
    anthropic.Anthropic | anthropic.AnthropicBedrockMantle | anthropic.AnthropicFoundry
)


class AnthropicLLMClient:
    def __init__(
        self, client: AnthropicSDKClient, *, provider: str, model: str, max_output_tokens: int
    ) -> None:
        self._client = client
        self.provider = provider
        self.model = model
        self._max_output_tokens = max_output_tokens

    def generate_structured(self, *, system: str, prompt: str, schema: type[T]) -> LLMResult[T]:
        started = time.perf_counter()
        try:
            # Structured outputs: the API constrains the answer to this JSON schema.
            # `transform_schema` adapts the Pydantic schema to the subset the API accepts.
            # We use `create` rather than the `parse` helper so we can check *why* the model
            # stopped before parsing: a refusal or truncation must not be reported as a
            # schema error.
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_output_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}
                },
            )
        except anthropic.APIConnectionError as exc:  # includes APITimeoutError
            raise LLMUnavailableError(f"{self.provider}: connection failed: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailableError(f"{self.provider}: rate limited") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:  # includes 529 overloaded
                raise LLMUnavailableError(
                    f"{self.provider}: server error {exc.status_code}"
                ) from exc
            raise LLMRequestError(f"{self.provider}: request rejected ({exc.status_code})") from exc
        latency_ms = round((time.perf_counter() - started) * 1000)

        # Check why the model stopped before trusting the content.
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise LLMOutputError(f"{self.provider}: model declined (category={category})")
        if response.stop_reason == "max_tokens":
            raise LLMOutputError(f"{self.provider}: output truncated at max_tokens")

        # Thinking blocks may precede the answer; the JSON is in the text block(s).
        text = "".join(block.text for block in response.content if block.type == "text")
        try:
            output = schema.model_validate_json(text)
        except ValidationError as exc:
            raise LLMOutputError(f"{self.provider}: output did not match the schema") from exc

        return LLMResult(
            output=output,
            usage=LLMUsage(response.usage.input_tokens, response.usage.output_tokens),
            model=response.model,
            latency_ms=latency_ms,
            request_id=getattr(response, "_request_id", None),
        )
