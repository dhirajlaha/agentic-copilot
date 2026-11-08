"""Builds the configured LLMClient. The only place that knows about vendors."""

import anthropic
import openai
from google import genai
from google.genai import types as genai_types

from app.core.config import EmbeddingSettings, LLMSettings
from app.db.models.code_chunk import EMBEDDING_DIM
from app.llm.anthropic_client import AnthropicLLMClient
from app.llm.client import LLMClient
from app.llm.embedding_client import EmbeddingClient
from app.llm.gemini_client import GeminiLLMClient
from app.llm.gemini_embeddings import GeminiEmbeddingClient
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
    if provider == "gemini":
        # google-genai takes the timeout in milliseconds; `attempts` includes the first try.
        options = genai_types.HttpOptions(
            timeout=round(settings.llm_timeout_seconds * 1000),
            retry_options=genai_types.HttpRetryOptions(attempts=settings.llm_max_retries + 1),
        )
        return GeminiLLMClient(
            genai.Client(api_key=key, http_options=options), provider=provider, **limits
        )
    # "openai": OpenAI, or any OpenAI-compatible endpoint via LLM_BASE_URL
    return OpenAICompatibleLLMClient(
        openai.OpenAI(api_key=key, base_url=settings.llm_base_url, **common),
        provider=provider,
        **limits,
    )


def build_embedding_client(settings: EmbeddingSettings) -> EmbeddingClient:
    if settings.embedding_dimensions != EMBEDDING_DIM:
        # The vector column has a fixed size; embeddings of another size cannot be stored.
        raise ValueError(
            f"EMBEDDING_DIMENSIONS={settings.embedding_dimensions} does not match the "
            f"database column ({EMBEDDING_DIM}). Changing it needs a migration and a re-index."
        )
    if settings.embedding_api_key is None:  # the settings validator prevents this
        raise ValueError("EMBEDDING_API_KEY is required")
    options = genai_types.HttpOptions(
        timeout=round(settings.embedding_timeout_seconds * 1000),
        # 429 is excluded on purpose: the SDK's few-second backoff cannot outlast a quota
        # window. The adapter waits for the delay Google asks for instead.
        retry_options=genai_types.HttpRetryOptions(
            attempts=settings.embedding_max_retries + 1,
            http_status_codes=[408, 500, 502, 503, 504],
        ),
    )
    return GeminiEmbeddingClient(
        genai.Client(api_key=settings.embedding_api_key.get_secret_value(), http_options=options),
        provider=settings.embedding_provider,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        batch_size=settings.embedding_batch_size,
        max_batch_tokens=settings.embedding_max_batch_tokens,
        rate_limit_retries=settings.embedding_rate_limit_retries,
    )
