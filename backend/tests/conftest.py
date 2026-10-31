"""Shared fixtures.

Tests run against a dedicated `copilot_test` database (created and migrated
automatically), never the development database. Each test runs in a transaction that is
rolled back, so tests are isolated and leave nothing behind.
"""

import os
from collections.abc import Iterator
from pathlib import Path

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

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.api.deps import get_github_oauth  # noqa: E402
from app.db.session import get_engine, get_session  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.fakes import FakeGitHub  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
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
def session() -> Iterator[Session]:
    connection = get_engine().connect()
    transaction = connection.begin()
    # create_savepoint: session.commit() (used by services) and DB errors only touch a
    # savepoint, so the outer rollback below still undoes everything.
    with Session(bind=connection, join_transaction_mode="create_savepoint") as s:
        yield s
    transaction.rollback()
    connection.close()


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def client(session: Session, github: FakeGitHub) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_github_oauth] = lambda: github
    # https base URL so the client's cookie jar accepts and returns Secure cookies.
    yield TestClient(app, base_url="https://api.test", follow_redirects=False)
