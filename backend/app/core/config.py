from functools import cached_property, lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV = SettingsConfigDict(env_file=".env", extra="ignore")


class DatabaseSettings(BaseSettings):
    """Just the database connection. Kept separate so tools that only need the database
    (Alembic migrations) do not also require the application's secrets."""

    model_config = _ENV

    # psycopg v3 driver, e.g. postgresql+psycopg://user:pass@host:5432/dbname
    database_url: SecretStr = Field(min_length=1)


LLMProvider = Literal["anthropic", "bedrock", "foundry", "openai", "gemini"]


class LLMSettings(BaseSettings):
    """Which LLM the agents use. Separate from `Settings` so the web app does not need
    LLM credentials to start, and agents do not need OAuth secrets.

    LLM_PROVIDER values:
    - anthropic: Claude API. Needs LLM_API_KEY.
    - bedrock: Claude on Amazon Bedrock. Needs AWS_REGION and AWS credentials.
    - foundry: Claude on Microsoft Foundry. Needs LLM_API_KEY and FOUNDRY_RESOURCE.
    - openai: OpenAI, or any OpenAI-compatible endpoint (Azure OpenAI, vLLM)
      selected with LLM_BASE_URL. Needs LLM_API_KEY.
    - gemini: Google Gemini through its native API. Needs LLM_API_KEY.
    """

    model_config = _ENV

    llm_provider: LLMProvider
    # Exact model id for the chosen provider, e.g. claude-opus-5-5, or on Bedrock
    # anthropic.claude-opus-5-5. No default: the model is a deliberate choice.
    llm_model: str = Field(min_length=1)
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    aws_region: str | None = None
    foundry_resource: str | None = None

    # Room for adaptive thinking plus the answer; responses stay under SDK HTTP timeouts.
    llm_max_output_tokens: int = Field(default=16000, gt=0)
    llm_timeout_seconds: float = Field(default=300.0, gt=0)
    # The SDKs retry connection errors, 408/409/429 and 5xx with backoff.
    llm_max_retries: int = Field(default=2, ge=0)

    @model_validator(mode="after")
    def _provider_requirements(self) -> "LLMSettings":
        missing: list[str] = []
        if (
            self.llm_provider in ("anthropic", "foundry", "openai", "gemini")
            and not self.llm_api_key
        ):
            missing.append("LLM_API_KEY")
        if self.llm_provider == "foundry" and not self.foundry_resource:
            missing.append("FOUNDRY_RESOURCE")
        if self.llm_provider == "bedrock" and not self.aws_region:
            missing.append("AWS_REGION")
        if missing:
            raise ValueError(f"LLM_PROVIDER={self.llm_provider} requires {', '.join(missing)}")
        if self.llm_base_url and self.llm_provider != "openai":
            raise ValueError("LLM_BASE_URL only applies to LLM_PROVIDER=openai")
        return self


class Settings(BaseSettings):
    """App settings from environment variables or `backend/.env`.

    Secrets and deployment-specific values have no defaults on purpose: the app refuses
    to start when they are missing instead of running with something insecure.
    """

    model_config = _ENV

    # Signs the short-lived OAuth state cookie. Generate with: openssl rand -hex 32
    secret_key: SecretStr = Field(min_length=32)

    # GitHub App credentials (client id/secret) and the callback URL registered on that app.
    github_client_id: str = Field(min_length=1)
    github_client_secret: SecretStr = Field(min_length=1)
    github_redirect_uri: str = Field(min_length=1)

    # Where the browser app lives; users are sent back here after login.
    frontend_url: str = Field(min_length=1)

    # Sessions and cookies.
    # The __Host- prefix makes browsers refuse the cookie unless it is Secure, Path=/ and
    # has no Domain, so a sibling subdomain cannot overwrite ("toss") our session cookie.
    session_cookie_name: str = "__Host-session"
    # Absolute lifetime from sign-in (no sliding renewal). Kept short because the browser
    # cookie lives as long as the session, and a laptop can stay open for days.
    session_ttl_seconds: int = Field(default=2 * 24 * 3600, gt=0)
    oauth_state_ttl_seconds: int = Field(default=600, gt=0)
    # Secure cookies are only sent over HTTPS (browsers exempt http://localhost).
    cookie_secure: bool = True

    # Swagger UI and the OpenAPI schema are off unless explicitly enabled (development).
    enable_docs: bool = False

    @model_validator(mode="after")
    def _host_prefix_requires_secure(self) -> "Settings":
        if self.session_cookie_name.startswith("__Host-") and not self.cookie_secure:
            raise ValueError("A __Host- session cookie requires COOKIE_SECURE=true")
        return self

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    @cached_property
    def frontend_origin(self) -> str:
        return self._origin(self.frontend_url)

    @cached_property
    def backend_origin(self) -> str:
        return self._origin(self.github_redirect_uri)


class WorkerSettings(BaseSettings):
    """How the run worker polls the queue and how long a claimed run may go silent."""

    model_config = _ENV

    worker_poll_interval_seconds: float = Field(default=2.0, gt=0)
    # A running run must be renewed within this window (it is renewed after every agent
    # step). Must exceed the slowest single step: an LLM call can take up to
    # LLM_TIMEOUT_SECONDS x (LLM_MAX_RETRIES + 1).
    run_lease_seconds: int = Field(default=1200, gt=0)


class GitHubAppSettings(BaseSettings):
    """The GitHub App itself (not the sign-in OAuth part): lets the backend act as an
    installation to read a connected repository. Used by the API (repo connect) and the
    worker (reading files for a run).

    The private key is a file, not an env var: it stays out of the repo, process listings
    and logs, and is checked at startup so a bad path fails fast instead of at first use.
    """

    model_config = _ENV

    # GitHub recommends the client id (not the numeric App ID) as the JWT issuer.
    github_client_id: str = Field(min_length=1)
    # The last part of https://github.com/apps/<slug>; builds the install link.
    github_app_slug: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,99}$")
    github_app_private_key_path: Path

    @model_validator(mode="after")
    def _key_is_a_readable_rsa_private_key(self) -> "GitHubAppSettings":
        path = self.github_app_private_key_path
        try:
            key = load_pem_private_key(path.read_bytes(), password=None)
        except OSError as exc:
            raise ValueError(f"GITHUB_APP_PRIVATE_KEY_PATH is not readable: {path}") from exc
        except (ValueError, TypeError) as exc:
            # Never include the file contents or the library's detail in the message.
            raise ValueError("GITHUB_APP_PRIVATE_KEY_PATH is not an unencrypted PEM key") from exc
        if not isinstance(key, RSAPrivateKey):
            raise ValueError("GITHUB_APP_PRIVATE_KEY_PATH must be an RSA private key")
        return self

    @cached_property
    def private_key_pem(self) -> SecretStr:
        return SecretStr(self.github_app_private_key_path.read_text())


@lru_cache
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()


@lru_cache
def get_github_app_settings() -> GitHubAppSettings:
    return GitHubAppSettings()  # type: ignore[call-arg]  # values come from the environment


@lru_cache
def get_database_settings() -> DatabaseSettings:
    return DatabaseSettings()  # type: ignore[call-arg]  # values come from the environment


@lru_cache
def get_llm_settings() -> LLMSettings:
    return LLMSettings()  # type: ignore[call-arg]  # values come from the environment


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
