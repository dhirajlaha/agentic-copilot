from functools import cached_property, lru_cache
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV = SettingsConfigDict(env_file=".env", extra="ignore")


class DatabaseSettings(BaseSettings):
    """Just the database connection. Kept separate so tools that only need the database
    (Alembic migrations) do not also require the application's secrets."""

    model_config = _ENV

    # psycopg v3 driver, e.g. postgresql+psycopg://user:pass@host:5432/dbname
    database_url: SecretStr = Field(min_length=1)


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
    session_ttl_seconds: int = Field(default=7 * 24 * 3600, gt=0)
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


@lru_cache
def get_database_settings() -> DatabaseSettings:
    return DatabaseSettings()  # type: ignore[call-arg]  # values come from the environment


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
