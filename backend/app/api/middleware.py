from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class OriginCheckMiddleware(BaseHTTPMiddleware):
    """Defense in depth against CSRF for cookie-authenticated requests.

    SameSite=Lax cookies already stop cross-site POSTs. This also rejects any
    state-changing browser request whose `Origin` header is not one of ours. Requests
    without an `Origin` (curl, server-to-server) are not browser CSRF vectors and pass.
    """

    def __init__(self, app: ASGIApp, allowed_origins: set[str]) -> None:
        super().__init__(app)
        self._allowed = allowed_origins

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        origin = request.headers.get("origin")
        if request.method not in SAFE_METHODS and origin and origin not in self._allowed:
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        return await call_next(request)
