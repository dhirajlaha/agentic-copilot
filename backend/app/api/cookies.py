from datetime import datetime

from fastapi import Response

from app.core.config import Settings

STATE_COOKIE_NAME = "oauth_state"
STATE_COOKIE_PATH = "/auth/github"


def set_session_cookie(
    response: Response, settings: Settings, token: str, expires_at: datetime
) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        expires=expires_at,
        httponly=True,  # not readable by JavaScript, so XSS cannot steal it
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def set_state_cookie(response: Response, settings: Settings, value: str) -> None:
    # Lax (not Strict): the cookie must be sent when GitHub redirects the browser back.
    response.set_cookie(
        STATE_COOKIE_NAME,
        value,
        max_age=settings.oauth_state_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=STATE_COOKIE_PATH,
    )


def clear_state_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        STATE_COOKIE_NAME,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=STATE_COOKIE_PATH,
    )
