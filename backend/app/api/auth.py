"""GitHub sign-in routes. Thin: parse the request, call AuthService, shape the response."""

import logging
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.api.cookies import (
    STATE_COOKIE_NAME,
    clear_session_cookie,
    clear_state_cookie,
    set_session_cookie,
    set_state_cookie,
)
from app.api.deps import AuthServiceDep, CurrentUser, SettingsDep
from app.core.exceptions import GitHubError, OAuthDeniedError, OAuthStateError
from app.db.models import User
from app.schemas.auth import UserOut
from app.services.auth_service import LoginResult

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/github/login", status_code=status.HTTP_302_FOUND)
def github_login(
    auth: AuthServiceDep,
    settings: SettingsDep,
    next_path: Annotated[str | None, Query(alias="next", max_length=2048)] = None,
) -> RedirectResponse:
    """Send the browser to GitHub. `next` is a same-site path to return to afterwards."""
    start = auth.start_login(next_path)
    response = RedirectResponse(start.authorization_url, status_code=status.HTTP_302_FOUND)
    set_state_cookie(response, settings, start.state_cookie)
    return response


@router.get("/github/callback", status_code=status.HTTP_302_FOUND)
def github_callback(
    request: Request,
    auth: AuthServiceDep,
    settings: SettingsDep,
    code: Annotated[str | None, Query(max_length=512)] = None,
    state: Annotated[str | None, Query(max_length=512)] = None,
    error: Annotated[str | None, Query(max_length=100)] = None,
) -> RedirectResponse:
    """GitHub redirects here. Every outcome ends as a redirect to the frontend: success
    goes to the requested page, failure to /login?error=<code> (details stay in the logs)."""
    failure: str | None = None
    result: LoginResult | None = None
    if error == "access_denied":
        failure = "access_denied"
    elif error or not code or not state:
        failure = "invalid_request"
    else:
        try:
            result = auth.complete_login(
                code=code,
                state=state,
                state_cookie=request.cookies.get(STATE_COOKIE_NAME),
            )
        except OAuthStateError:
            logger.warning("OAuth callback rejected: invalid state")
            failure = "invalid_state"
        except OAuthDeniedError:
            failure = "access_denied"
        except GitHubError as exc:
            logger.error("GitHub sign-in failed: %s", exc)
            failure = "github_error"

    if result is None:
        target = f"{settings.frontend_origin}/login?{urlencode({'error': failure})}"
        response = RedirectResponse(target, status_code=status.HTTP_302_FOUND)
    else:
        response = RedirectResponse(
            f"{settings.frontend_origin}{result.next_path}",
            status_code=status.HTTP_302_FOUND,
        )
        set_session_cookie(response, settings, result.session_token, result.expires_at)
    clear_state_cookie(response, settings)
    return response


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> User:
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, auth: AuthServiceDep, settings: SettingsDep) -> Response:
    auth.logout(request.cookies.get(settings.session_cookie_name))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response, settings)
    return response
