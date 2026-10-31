"""The Anthropic adapter against the real `anthropic` SDK, with a fake HTTP transport.

This checks what we actually send over the wire and how real API responses and errors
are mapped, without network access or an API key.
"""

import json

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from app.llm.anthropic_client import AnthropicLLMClient
from app.llm.client import LLMOutputError, LLMRequestError, LLMUnavailableError


class Answer(BaseModel):
    city: str
    population: int


def message(text: str, stop_reason: str = "end_turn", **extra) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 7},
        **extra,
    }


def make_client(handler) -> AnthropicLLMClient:
    sdk = anthropic.Anthropic(
        api_key="test-key",
        max_retries=0,  # surface errors immediately in tests
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return AnthropicLLMClient(
        sdk, provider="anthropic", model="claude-opus-5-5", max_output_tokens=1000
    )


def call(client):
    return client.generate_structured(system="Be precise.", prompt="Capital?", schema=Answer)


def test_success_sends_structured_output_request_and_maps_result():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json=message('{"city": "Paris", "population": 2100000}'),
            headers={"request-id": "req_abc"},
        )

    result = call(make_client(handler))

    assert result.output == Answer(city="Paris", population=2100000)
    assert (result.usage.input_tokens, result.usage.output_tokens) == (11, 7)
    assert result.usage.total_tokens == 18
    assert result.model == "claude-opus-5-5"
    assert result.request_id == "req_abc"

    body = seen["body"]
    assert seen["url"].endswith("/v1/messages")
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 1000
    assert body["system"] == "Be precise."
    assert body["messages"] == [{"role": "user", "content": "Capital?"}]
    assert body["output_config"]["format"]["type"] == "json_schema"  # structured outputs
    assert "tool_choice" not in body  # no forced tool use (rejected by current models)


def test_refusal_is_an_output_error():
    def handler(request):
        return httpx2.Response(
            200,
            json=message(
                "", stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber"}
            ),
        )

    with pytest.raises(LLMOutputError, match="declined"):
        call(make_client(handler))


def test_truncation_is_an_output_error():
    def handler(request):
        return httpx2.Response(200, json=message('{"city": "Par', stop_reason="max_tokens"))

    with pytest.raises(LLMOutputError, match="truncated"):
        call(make_client(handler))


def test_thinking_blocks_before_the_answer_are_skipped():
    def handler(request):
        body = message('{"city": "Paris", "population": 1}')
        body["content"].insert(0, {"type": "thinking", "thinking": "", "signature": "sig"})
        return httpx2.Response(200, json=body)

    assert call(make_client(handler)).output.city == "Paris"


def test_schema_mismatch_is_an_output_error():
    def handler(request):
        return httpx2.Response(200, json=message('{"city": "Paris"}'))  # missing field

    with pytest.raises(LLMOutputError, match="schema"):
        call(make_client(handler))


@pytest.mark.parametrize(
    ("status", "error_type", "expected"),
    [
        (429, "rate_limit_error", LLMUnavailableError),
        (500, "api_error", LLMUnavailableError),
        (529, "overloaded_error", LLMUnavailableError),
        (400, "invalid_request_error", LLMRequestError),
        (401, "authentication_error", LLMRequestError),
        (404, "not_found_error", LLMRequestError),
    ],
)
def test_http_errors_are_classified(status, error_type, expected):
    def handler(request):
        return httpx2.Response(
            status, json={"type": "error", "error": {"type": error_type, "message": "x"}}
        )

    with pytest.raises(expected) as info:
        call(make_client(handler))
    assert info.value.retryable is (expected is LLMUnavailableError)


def test_network_failure_is_unavailable():
    def handler(request):
        raise httpx2.ConnectError("down")

    with pytest.raises(LLMUnavailableError):
        call(make_client(handler))
