"""Builds the configured LLMClient. The only place that knows about vendors."""

import anthropic
import openai

from app.core.config import LLMSettings
from app.llm.anthropic_client import AnthropicLLMClient
from app.llm.client import LLMClient
from app.llm.openai_client import OpenAICompatibleLLMClient


def build_llm_client(settings: LLMSettings) -> LLMClient:
    common = {"timeout": settings.llm_timeout_seconds, "max_retries": settings.llm_max_retries}
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
    provider = settings.llm_provider
    limits = {"model": settings.llm_model, "max_output_tokens": settings.llm_max_output_tokens}

    if provider == "anthropic":
        return AnthropicLLMClient(
            anthropic.Anthropic(api_key=key, **common), provider=provider, **limits
        )
    if provider == "bedrock":
        # AWS credentials come from the standard AWS chain (env vars, profile, IAM role).
        return AnthropicLLMClient(
            anthropic.AnthropicBedrockMantle(aws_region=settings.aws_region, **common),
            provider=provider,
            **limits,
        )
    if provider == "foundry":
        return AnthropicLLMClient(
            anthropic.AnthropicFoundry(api_key=key, resource=settings.foundry_resource, **common),
            provider=provider,
            **limits,
        )
    # "openai": OpenAI, or any OpenAI-compatible endpoint via LLM_BASE_URL
    return OpenAICompatibleLLMClient(
        openai.OpenAI(api_key=key, base_url=settings.llm_base_url, **common),
        provider=provider,
        **limits,
    )
