"""The Gemini embedding adapter against the real `google-genai` SDK, with a fake transport."""

import json

import httpx
import pytest
from google import genai
from google.genai import types

from app.llm.client import LLMOutputError, LLMRequestError, LLMUnavailableError
from app.llm.gemini_embeddings import GeminiEmbeddingClient

DIMS = 4


def vectors(n: int, dims: int = DIMS) -> dict:
    return {"embeddings": [{"values": [float(i + 1)] * dims} for i in range(n)]}


def make_client(
    handler, batch_size: int = 2, sleep=lambda seconds: None, **kwargs
) -> GeminiEmbeddingClient:
    options = types.HttpOptions(
        httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
        retry_options=types.HttpRetryOptions(attempts=1),
    )
    sdk = genai.Client(api_key="test-key", http_options=options)
    return GeminiEmbeddingClient(
        sdk,
        provider="gemini",
        model="gemini-embedding-001",
        dimensions=DIMS,
        batch_size=batch_size,
        sleep=sleep,
        **kwargs,
    )


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content)


def test_documents_are_embedded_in_batches_and_order_is_kept():
    batches = []

    def handler(request):
        body = body_of(request)
        batches.append(body)
        return httpx.Response(200, json=vectors(len(body["requests"])))

    result = make_client(handler, batch_size=2).embed_documents(["a", "b", "c", "d", "e"])

    assert len(result) == 5 and all(len(v) == DIMS for v in result)
    assert [len(b["requests"]) for b in batches] == [2, 2, 1]
    request = batches[0]["requests"][0]
    assert request["taskType"] == "RETRIEVAL_DOCUMENT"
    assert request["outputDimensionality"] == DIMS
    assert request["model"].endswith("gemini-embedding-001")


def test_queries_use_the_code_retrieval_task_type():
    seen = {}

    def handler(request):
        seen.update(body_of(request)["requests"][0])
        return httpx.Response(200, json=vectors(1))

    assert len(make_client(handler).embed_query("where is login handled")) == DIMS
    assert seen["taskType"] == "CODE_RETRIEVAL_QUERY"


def test_no_documents_means_no_requests():
    def handler(request):
        raise AssertionError("must not call the API")

    assert make_client(handler).embed_documents([]) == []


def test_empty_text_is_refused():
    with pytest.raises(ValueError, match="empty"):
        make_client(lambda r: httpx.Response(200, json=vectors(1))).embed_documents([" \n"])


def test_a_count_mismatch_is_rejected_not_misaligned():
    # The provider merged inputs into one vector: attaching it to a chunk would be wrong.
    with pytest.raises(LLMOutputError, match="1 embeddings for 2 texts"):
        make_client(lambda r: httpx.Response(200, json=vectors(1))).embed_documents(["a", "b"])


def test_wrong_sized_vectors_are_rejected():
    with pytest.raises(LLMOutputError, match="unusable"):
        make_client(lambda r: httpx.Response(200, json=vectors(1, dims=3))).embed_documents(["a"])


def test_a_missing_embeddings_field_is_rejected():
    with pytest.raises(LLMOutputError):
        make_client(lambda r: httpx.Response(200, json={})).embed_documents(["a"])


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, LLMUnavailableError), (503, LLMUnavailableError), (400, LLMRequestError),
     (403, LLMRequestError), (404, LLMRequestError)],
)  # fmt: skip
def test_http_errors_are_classified(status, expected):
    def handler(request):
        return httpx.Response(status, json={"error": {"code": status, "message": "x"}})

    with pytest.raises(expected):
        make_client(handler).embed_documents(["a"])


def test_network_failures_are_unavailable():
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(LLMUnavailableError):
        make_client(handler).embed_documents(["a"])


# ---- token budget ---------------------------------------------------------------------


def test_requests_are_split_to_stay_under_the_token_budget():
    batches = []

    def handler(request):
        body = body_of(request)
        batches.append(len(body["requests"]))
        return httpx.Response(200, json=vectors(len(body["requests"])))

    # Each text is ~100 estimated tokens; a 250-token budget fits two per request even
    # though the count limit would allow ten.
    texts = ["x" * 297] * 5
    make_client(handler, batch_size=10, max_batch_tokens=250).embed_documents(texts)
    assert batches == [2, 2, 1]


def test_one_oversized_text_still_gets_its_own_request():
    batches = []

    def handler(request):
        batches.append(len(body_of(request)["requests"]))
        return httpx.Response(200, json=vectors(batches[-1]))

    make_client(handler, batch_size=10, max_batch_tokens=50).embed_documents(["y" * 900, "z"])
    assert batches == [1, 1]


# ---- rate limits ----------------------------------------------------------------------


def rate_limited(delay: str | None, message: str = "quota exceeded") -> httpx.Response:
    details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]
    body = {"error": {"code": 429, "message": message, "status": "RESOURCE_EXHAUSTED"}}
    if delay:
        body["error"]["details"] = details
    return httpx.Response(429, json=body)


def flaky(failures: int, first: httpx.Response):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) <= failures:
            return first
        return httpx.Response(200, json=vectors(len(body_of(request)["requests"])))

    return handler, calls


def test_a_rate_limit_is_waited_out_for_as_long_as_google_asks():
    handler, calls = flaky(2, rate_limited("29s"))
    waits = []

    result = make_client(handler, sleep=waits.append).embed_documents(["a"])

    assert len(result) == 1 and len(calls) == 3
    assert waits == [30.0, 30.0]  # 29s asked for, plus a second of margin


def test_the_wait_is_read_from_the_message_when_there_is_no_retry_detail():
    handler, _ = flaky(1, rate_limited(None, "Quota exceeded. Please retry in 12.5s."))
    waits = []
    make_client(handler, sleep=waits.append).embed_documents(["a"])
    assert waits == [13.5]


def test_a_default_wait_is_used_when_google_gives_no_hint():
    handler, _ = flaky(1, rate_limited(None, "Quota exceeded."))
    waits = []
    make_client(handler, sleep=waits.append).embed_documents(["a"])
    assert waits == [31.0]


def test_an_enormous_requested_wait_is_capped():
    handler, _ = flaky(1, rate_limited("3600s"))
    waits = []
    make_client(handler, sleep=waits.append).embed_documents(["a"])
    assert waits == [90.0]


def test_giving_up_after_the_retries_is_an_unavailable_error():
    handler, calls = flaky(99, rate_limited("1s"))
    waits = []
    with pytest.raises(LLMUnavailableError, match="rate limited"):
        make_client(handler, sleep=waits.append, rate_limit_retries=2).embed_documents(["a"])
    assert len(calls) == 3 and len(waits) == 2


def test_other_errors_do_not_wait_or_retry():
    waits = []
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": {"code": 400, "message": "bad"}})

    with pytest.raises(LLMRequestError):
        make_client(handler, sleep=waits.append).embed_documents(["a"])
    assert waits == [] and len(calls) == 1


def test_queries_also_wait_out_rate_limits():
    handler, _ = flaky(1, rate_limited("5s"))
    waits = []
    assert len(make_client(handler, sleep=waits.append).embed_query("q")) == DIMS
    assert waits == [6.0]
