import anthropic
import openai
import pytest
from pydantic import ValidationError

from app.core.config import LLMSettings
from app.llm.anthropic_client import AnthropicLLMClient
from app.llm.factory import build_llm_client
from app.llm.openai_client import OpenAICompatibleLLMClient


def settings(**values) -> LLMSettings:
    return LLMSettings(**{"llm_model": "m", **values}, _env_file=None)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # The test environment pins LLM_* values; these tests need a blank slate.
    for name in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "AWS_REGION",
        "FOUNDRY_RESOURCE",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"llm_provider": "anthropic"}, "LLM_API_KEY"),
        ({"llm_provider": "openai"}, "LLM_API_KEY"),
        ({"llm_provider": "foundry", "llm_api_key": "k"}, "FOUNDRY_RESOURCE"),
        ({"llm_provider": "bedrock"}, "AWS_REGION"),
        (
            {"llm_provider": "anthropic", "llm_api_key": "k", "llm_base_url": "http://x"},
            "LLM_BASE_URL",
        ),
        ({"llm_provider": "mistral", "llm_api_key": "k"}, "llm_provider"),
    ],
)
def test_invalid_configurations_fail_fast(values, message):
    with pytest.raises(ValidationError, match=message):
        settings(**values)


def test_model_is_required():
    with pytest.raises(ValidationError):
        LLMSettings(llm_provider="anthropic", llm_api_key="k", _env_file=None)


@pytest.mark.parametrize(
    ("values", "adapter", "sdk_class"),
    [
        (
            {"llm_provider": "anthropic", "llm_api_key": "k"},
            AnthropicLLMClient,
            anthropic.Anthropic,
        ),
        (
            {"llm_provider": "bedrock", "aws_region": "us-east-1"},
            AnthropicLLMClient,
            anthropic.AnthropicBedrockMantle,
        ),
        (
            {"llm_provider": "foundry", "llm_api_key": "k", "foundry_resource": "my-res"},
            AnthropicLLMClient,
            anthropic.AnthropicFoundry,
        ),
        ({"llm_provider": "openai", "llm_api_key": "k"}, OpenAICompatibleLLMClient, openai.OpenAI),
    ],
)
def test_factory_builds_the_right_client(values, adapter, sdk_class, monkeypatch):
    # Bedrock resolves AWS credentials at construction; give it dummy ones.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    client = build_llm_client(settings(**values, llm_timeout_seconds=30, llm_max_retries=4))
    assert isinstance(client, adapter)
    assert isinstance(client._client, sdk_class)
    assert client._client.max_retries == 4
    assert client.provider == values["llm_provider"] and client.model == "m"


def test_openai_base_url_is_applied():
    client = build_llm_client(
        settings(llm_provider="openai", llm_api_key="k", llm_base_url="http://localhost:8001/v1")
    )
    assert str(client._client.base_url).startswith("http://localhost:8001/v1")
