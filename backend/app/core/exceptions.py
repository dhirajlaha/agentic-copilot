"""Domain exceptions. Services raise these; `app.api.errors` maps them to HTTP responses,
so business code never imports FastAPI."""


class AppError(Exception):
    """Base class for expected, handled errors."""


class NotFoundError(AppError):
    def __init__(self, what: str = "Resource") -> None:
        super().__init__(f"{what} not found")


class AuthenticationError(AppError):
    """The caller is not signed in (missing, unknown or expired session)."""


class OAuthStateError(AppError):
    """The OAuth `state` was missing, tampered with, expired or did not match."""


class OAuthDeniedError(AppError):
    """The user declined the GitHub authorization prompt."""


class GitHubError(AppError):
    """GitHub rejected a request or was unreachable."""
