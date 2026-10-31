from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_database_settings


@lru_cache
def get_engine() -> Engine:
    # pool_pre_ping drops dead connections (e.g. after a DB restart) instead of erroring.
    return create_engine(
        get_database_settings().database_url.get_secret_value(), pool_pre_ping=True
    )


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one DB session per request, always closed afterwards.

    Services own the transaction: they call `commit()` when a use case succeeds.
    """
    with get_session_factory()() as session:
        yield session
