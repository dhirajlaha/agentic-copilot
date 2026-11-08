"""Shared fixtures.

Tests that need the database (anything using the `session` or `client` fixture) run
against a dedicated `copilot_test` database, created and migrated on first use, never
the development database. Pure unit tests (agents, LLM adapters, security helpers) do not
touch Postgres at all. Each test runs in a transaction that is
rolled back, so tests are isolated and leave nothing behind.
"""

import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# Must be set before any `app` import: settings are read from the environment.
TEST_DB_NAME = "copilot_test"
_BASE_URL = os.environ.get(
    "TEST_DATABASE_SERVER_URL", "postgresql+psycopg://copilot:copilot@localhost:5432"
)
os.environ["DATABASE_URL"] = f"{_BASE_URL}/{TEST_DB_NAME}"
os.environ["SECRET_KEY"] = "test-secret-key-test-secret-key-0123456789"
os.environ["GITHUB_CLIENT_ID"] = "test-client-id"
os.environ["GITHUB_CLIENT_SECRET"] = "test-client-secret"
os.environ["GITHUB_REDIRECT_URI"] = "https://api.test/auth/github/callback"
os.environ["FRONTEND_URL"] = "https://app.test"
os.environ["COOKIE_SECURE"] = "true"
# Pin every optional setting too, so a developer's local .env can never change test results.
os.environ["ENABLE_DOCS"] = "false"
os.environ["SESSION_COOKIE_NAME"] = "__Host-session"
os.environ["SESSION_TTL_SECONDS"] = "604800"
os.environ["OAUTH_STATE_TTL_SECONDS"] = "600"
os.environ["LLM_PROVIDER"] = "anthropic"
os.environ["LLM_MODEL"] = "test-model"
os.environ["LLM_API_KEY"] = "test-llm-key"
# A throwaway RSA key stands in for the GitHub App's private key.
TEST_APP_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KEY_DIR = Path(tempfile.mkdtemp(prefix="copilot-test-key-"))
TEST_APP_KEY_PATH = _KEY_DIR / "app.pem"
TEST_APP_KEY_PATH.write_bytes(
    TEST_APP_KEY.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
)
os.environ["GITHUB_APP_SLUG"] = "test-app"
os.environ["GITHUB_APP_PRIVATE_KEY_PATH"] = str(TEST_APP_KEY_PATH)
for _name in ("LLM_BASE_URL", "AWS_REGION", "FOUNDRY_RESOURCE"):
    os.environ.pop(_name, None)

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Connection, create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.api.deps import get_github_app, get_github_oauth  # noqa: E402
from app.db.session import get_engine, get_session  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.fakes import FakeGitHub, FakeGitHubApp  # noqa: E402


@pytest.fixture(scope="session")
def _test_database() -> None:
    """Create the test database if needed and migrate it to the latest revision."""
    admin = create_engine(f"{_BASE_URL}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": TEST_DB_NAME}
        )
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    admin.dispose()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")


@pytest.fixture
def connection(_test_database: None) -> Iterator[Connection]:
    """One connection per test, inside a transaction that is rolled back at the end."""
    conn = get_engine().connect()
    transaction = conn.begin()
    yield conn
    transaction.rollback()
    conn.close()


@pytest.fixture
def session_factory(connection: Connection) -> Callable[[], Session]:
    """Sessions bound to the test connection. `join_transaction_mode="create_savepoint"`
    turns their commit() into savepoints, so code that commits (services, the worker)
    still leaves nothing behind, and DB errors do not break the outer rollback."""
    # Same options as the production session factory (app/db/session.py).
    return lambda: Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        autoflush=False,
        expire_on_commit=False,
    )


@pytest.fixture
def session(session_factory: Callable[[], Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def github_app() -> FakeGitHubApp:
    return FakeGitHubApp()


@pytest.fixture
def client(
    session: Session,
    session_factory: Callable[[], Session],
    github: FakeGitHub,
    github_app: FakeGitHubApp,
) -> Iterator[TestClient]:
    app = create_app()

    def session_per_request() -> Iterator[Session]:
        # Like production: a fresh session for every request (no stale cached objects).
        # It shares the test connection, so it sees data the test created in `session`.
        with session_factory() as request_session:
            yield request_session

    app.dependency_overrides[get_session] = session_per_request
    app.dependency_overrides[get_github_oauth] = lambda: github
    app.dependency_overrides[get_github_app] = lambda: github_app
    # https base URL so the client's cookie jar accepts and returns Secure cookies.
    yield TestClient(app, base_url="https://api.test", follow_redirects=False)
