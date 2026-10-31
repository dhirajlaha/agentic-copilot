from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, health, tickets
from app.api.errors import register_exception_handlers
from app.api.middleware import OriginCheckMiddleware
from app.core.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # One pooled HTTP client for all outbound calls (GitHub), closed on shutdown.
    with httpx.Client(timeout=httpx.Timeout(10.0), follow_redirects=False) as client:
        app.state.http_client = client
        yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Agentic Dev/Test Copilot",
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )

    # Middleware added last runs first: CORS answers preflights before the origin check.
    app.add_middleware(
        OriginCheckMiddleware,
        allowed_origins={settings.frontend_origin, settings.backend_origin},
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )
    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(tickets.router)
    return app


app = create_app()
