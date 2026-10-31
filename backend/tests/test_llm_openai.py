"""The OpenAI-compatible adapter against the real `openai` SDK, with a fake transport."""

import json

import httpx2
import openai
import pytest
from pydantic import BaseModel

from app.llm.client import LLMOutputError, LLMRequestError, LLMUnavailableError
from app.llm.openai_client import OpenAICompatibleLLMClient


class Answer(BaseModel):
    city: str
    population: int


def completion(content: str | None, finish_reason: str = "stop", refusal: str | None = None):
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-test",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
        "usage": {"prompt_tokens": 13, "completion_tokens": 5, "total_tokens": 18},
    }


def make_client(handler, base_url: str | None = None) -> OpenAICompatibleLLMClient:
    sdk = openai.OpenAI(
        api_key="test-key",
        base_url=base_url,
        max_retries=0,
        http_client=openai.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return OpenAICompatibleLLMClient(
        sdk, provider="openai", model="gpt-test", max_output_tokens=500
    )


def call(client):
    return client.generate_structured(system="Be precise.", prompt="Capital?", schema=Answer)


def test_success_sends_json_schema_and_maps_result():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=completion('{"city": "Paris", "population": 2100000}'))

    result = call(make_client(handler))
    assert result.output == Answer(city="Paris", population=2100000)
    assert (result.usage.input_tokens, result.usage.output_tokens) == (13, 5)
    assert result.model == "gpt-test"

    body = seen["body"]
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert body["messages"][0] == {"role": "system", "content": "Be precise."}
    assert body["response_format"]["type"] == "json_schema"
    assert body["max_completion_tokens"] == 500


def test_base_url_targets_compatible_providers():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx2.Response(200, json=completion('{"city": "Rome", "population": 1}'))

    gemini = "https://generativelanguage.googleapis.com/v1beta/openai/"
    call(make_client(handler, base_url=gemini))
    assert seen["url"] == gemini + "chat/completions"


def test_missing_usage_is_reported_as_unknown():
    def handler(request):
        body = completion('{"city": "Rome", "population": 1}')
        body.pop("usage")
        return httpx2.Response(200, json=body)

    result = call(make_client(handler))
    assert result.usage.input_tokens is None and result.usage.total_tokens is None


def test_refusal_is_an_output_error():
    def handler(request):
        return httpx2.Response(200, json=completion(None, refusal="I can't help with that."))

    with pytest.raises(LLMOutputError, match="declined"):
        call(make_client(handler))


def test_truncation_is_an_output_error():
    def handler(request):
        return httpx2.Response(200, json=completion('{"city": "Ro', finish_reason="length"))

    with pytest.raises(LLMOutputError, match="truncated"):
        call(make_client(handler))


def test_schema_mismatch_is_an_output_error():
    def handler(request):
        return httpx2.Response(200, json=completion('{"city": "Rome"}'))

    with pytest.raises(LLMOutputError):
        call(make_client(handler))


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, LLMUnavailableError),
        (503, LLMUnavailableError),
        (400, LLMRequestError),
        (401, LLMRequestError),
    ],
)
def test_http_errors_are_classified(status, expected):
    def handler(request):
        return httpx2.Response(status, json={"error": {"message": "x", "type": "x", "code": None}})

    with pytest.raises(expected):
        call(make_client(handler))


def test_network_failure_is_unavailable():
    def handler(request):
        raise httpx2.ConnectError("down")

    with pytest.raises(LLMUnavailableError):
        call(make_client(handler))
