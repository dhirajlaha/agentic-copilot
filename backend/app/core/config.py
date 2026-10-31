from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App settings, loaded from environment variables or a local `.env` file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # psycopg v3 driver. Defaults match the local docker container in README.md.
    database_url: str = "postgresql+psycopg://copilot:copilot@localhost:5432/copilot"


settings = Settings()
