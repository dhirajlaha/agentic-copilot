import pytest
from pydantic import ValidationError

from app.core.config import DatabaseSettings, GitHubAppSettings, Settings

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


# ---- GitHub App settings -------------------------------------------------------------


def app_settings(**overrides):
    values = {"github_client_id": "id", "github_app_slug": "my-app"} | overrides
    return GitHubAppSettings(**values, _env_file=None)


def test_github_app_settings_load_a_real_key(monkeypatch):
    s = app_settings()
    assert s.github_app_slug == "my-app"
    assert s.private_key_pem.get_secret_value().startswith("-----BEGIN")
    assert "BEGIN" not in repr(s.private_key_pem)  # never printed


def test_github_app_key_is_required(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH")
    with pytest.raises(ValidationError):
        app_settings()


def test_missing_key_file_fails_fast(tmp_path):
    with pytest.raises(ValidationError, match="not readable"):
        app_settings(github_app_private_key_path=tmp_path / "nope.pem")


def test_garbage_key_fails_without_leaking_contents(tmp_path):
    bad = tmp_path / "bad.pem"
    bad.write_text("super-secret-garbage")
    with pytest.raises(ValidationError) as exc:
        app_settings(github_app_private_key_path=bad)
    assert "not an unencrypted PEM key" in str(exc.value)
    assert "super-secret-garbage" not in str(exc.value)


def test_non_rsa_key_is_rejected(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = tmp_path / "ec.pem"
    key.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(ValidationError, match="RSA"):
        app_settings(github_app_private_key_path=key)


@pytest.mark.parametrize("slug", ["", "has space", "../x", "a/b", "-lead"])
def test_slug_must_be_url_safe(slug):
    with pytest.raises(ValidationError):
        app_settings(github_app_slug=slug)
