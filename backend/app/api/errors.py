"""Maps domain exceptions to HTTP responses (kept out of the business code)."""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.exceptions import (
    AppError,
    AuthenticationError,
    GitHubError,
    NotFoundError,
)

logger = logging.getLogger(__name__)

_STATUS: dict[type[AppError], int] = {
    NotFoundError: 404,
    AuthenticationError: 401,
    GitHubError: 502,
}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        status_code = next((code for cls, code in _STATUS.items() if isinstance(exc, cls)), 400)
        if status_code >= 500:
            # Log the real cause; do not leak upstream details to the client.
            logger.error("upstream error on %s %s: %s", request.method, request.url.path, exc)
            detail = "Upstream service error"
        else:
            detail = str(exc) or "Request failed"
        headers = {"WWW-Authenticate": "Cookie"} if status_code == 401 else None
        return JSONResponse({"detail": detail}, status_code=status_code, headers=headers)
