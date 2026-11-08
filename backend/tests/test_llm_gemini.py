"""The Gemini adapter against the real `google-genai` SDK, with a fake transport."""

import json

import httpx
import pytest
from google import genai
from google.genai import types
from pydantic import BaseModel

from app.llm.client import LLMOutputError, LLMRequestError, LLMUnavailableError
from app.llm.gemini_client import GeminiLLMClient


class Answer(BaseModel):
    city: str
    population: int


def reply(text: str | None, finish: str = "STOP", usage: bool = True) -> dict:
    parts = [{"text": text}] if text is not None else []
    body: dict = {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
        "modelVersion": "gemini-test-001",
        "responseId": "resp-1",
    }
    if usage:
        body["usageMetadata"] = {
            "promptTokenCount": 13,
            "candidatesTokenCount": 5,
            "thoughtsTokenCount": 7,
            "totalTokenCount": 25,
        }
    return body


def make_client(handler) -> GeminiLLMClient:
    options = types.HttpOptions(
        httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
        retry_options=types.HttpRetryOptions(attempts=1),
    )
    sdk = genai.Client(api_key="test-key", http_options=options)
    return GeminiLLMClient(sdk, provider="gemini", model="gemini-test", max_output_tokens=500)


def call(client):
    return client.generate_structured(system="Be precise.", prompt="Capital?", schema=Answer)


def test_success_sends_schema_and_maps_result():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=reply('{"city": "Paris", "population": 2100000}'))

    result = call(make_client(handler))
    assert result.output == Answer(city="Paris", population=2100000)
    assert result.model == "gemini-test-001"
    assert result.request_id == "resp-1"
    # Thinking tokens are billed as output, so they are counted with it: 5 + 7.
    assert (result.usage.input_tokens, result.usage.output_tokens) == (13, 12)

    assert seen["url"].endswith("/models/gemini-test:generateContent")
    body = seen["body"]
    assert body["systemInstruction"]["parts"][0]["text"] == "Be precise."
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["maxOutputTokens"] == 500
    assert config["responseJsonSchema"]["properties"]["city"]["type"] == "string"


def test_missing_usage_is_reported_as_unknown():
    def handler(request):
        return httpx.Response(200, json=reply('{"city": "Rome", "population": 1}', usage=False))

    result = call(make_client(handler))
    assert result.usage.input_tokens is None and result.usage.total_tokens is None


def test_truncation_is_an_output_error():
    def handler(request):
        return httpx.Response(200, json=reply('{"city": "Ro', finish="MAX_TOKENS"))

    with pytest.raises(LLMOutputError, match="truncated"):
        call(make_client(handler))


def test_safety_stop_is_an_output_error():
    def handler(request):
        return httpx.Response(200, json=reply(None, finish="SAFETY"))

    with pytest.raises(LLMOutputError, match="SAFETY"):
        call(make_client(handler))


def test_blocked_prompt_is_an_output_error():
    def handler(request):
        return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})

    with pytest.raises(LLMOutputError, match="blocked"):
        call(make_client(handler))


def test_empty_answer_is_an_output_error():
    def handler(request):
        return httpx.Response(200, json=reply(None))

    with pytest.raises(LLMOutputError):
        call(make_client(handler))


def test_schema_mismatch_is_an_output_error():
    def handler(request):
        return httpx.Response(200, json=reply('{"city": "Rome"}'))

    with pytest.raises(LLMOutputError, match="schema"):
        call(make_client(handler))


def test_non_json_is_an_output_error():
    def handler(request):
        return httpx.Response(200, json=reply("Paris, probably."))

    with pytest.raises(LLMOutputError, match="schema"):
        call(make_client(handler))


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (408, LLMUnavailableError),
        (429, LLMUnavailableError),
        (500, LLMUnavailableError),
        (503, LLMUnavailableError),
        (400, LLMRequestError),
        (401, LLMRequestError),
        (403, LLMRequestError),
        (404, LLMRequestError),
    ],
)
def test_http_errors_are_classified(status, expected):
    def handler(request):
        return httpx.Response(status, json={"error": {"code": status, "message": "x"}})

    with pytest.raises(expected):
        call(make_client(handler))


def test_network_failure_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(LLMUnavailableError):
        call(make_client(handler))


def test_timeout_is_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(LLMUnavailableError):
        call(make_client(handler))
