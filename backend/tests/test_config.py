import pytest
from pydantic import ValidationError

from app.core.config import DatabaseSettings, Settings

BASE = {
    "secret_key": "k" * 32,
    "github_client_id": "id",
    "github_client_secret": "secret",
    "github_redirect_uri": "https://api.test/auth/github/callback",
    "frontend_url": "https://app.test",
}


def test_valid_settings_and_origins():
    s = Settings(**BASE, _env_file=None)
    assert s.frontend_origin == "https://app.test"
    assert s.backend_origin == "https://api.test"
    assert s.session_cookie_name == "__Host-session"
    assert s.enable_docs is False


@pytest.mark.parametrize("missing", ["secret_key", "github_client_id", "github_client_secret"])
def test_required_values_have_no_defaults(missing, monkeypatch):
    monkeypatch.delenv(missing.upper())  # the test environment provides them otherwise
    values = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(ValidationError):
        Settings(**values, _env_file=None)


def test_short_secret_key_is_rejected():
    with pytest.raises(ValidationError):
        Settings(**{**BASE, "secret_key": "short"}, _env_file=None)


def test_host_prefixed_cookie_requires_secure():
    with pytest.raises(ValidationError):
        Settings(**BASE, cookie_secure=False, _env_file=None)
    Settings(**BASE, cookie_secure=False, session_cookie_name="session", _env_file=None)


def test_database_settings_need_only_the_database_url(monkeypatch):
    """Migrations must not require OAuth secrets."""
    for name in ("SECRET_KEY", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "FRONTEND_URL"):
        monkeypatch.delenv(name)
    assert DatabaseSettings(_env_file=None).database_url.get_secret_value()
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(ValidationError):
        DatabaseSettings(_env_file=None)
