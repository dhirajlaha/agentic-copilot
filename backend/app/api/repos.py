"""Repository connection routes. Thin: parse, call RepoService, shape the response."""

import logging
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.api.cookies import STATE_COOKIE_NAME, clear_state_cookie, set_state_cookie
from app.api.deps import OAUTH_STATE_SALT, CurrentUser, RepoServiceDep, SettingsDep, UserOrNone
from app.core.exceptions import (
    GitHubError,
    InstallationVerificationError,
    OAuthStateError,
)
from app.core.security import SignedCookieCodec
from app.db.models import RepoConnection, User
from app.schemas.repo import (
    AvailableInstallationOut,
    AvailableRepoOut,
    RepoConnect,
    RepoOut,
)
from app.services.repo_service import DEFAULT_NEXT, VERIFY_PURPOSE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/repos", tags=["repos"])


@router.get("/install", status_code=status.HTTP_302_FOUND)
def install(user: CurrentUser, repos: RepoServiceDep) -> RedirectResponse:
    """Send the browser to GitHub to install the app or change its repositories."""
    return RedirectResponse(repos.install_url(), status_code=status.HTTP_302_FOUND)


@router.get("/installed", status_code=status.HTTP_302_FOUND)
def installed(
    request: Request,
    repos: RepoServiceDep,
    settings: SettingsDep,
    user: UserOrNone,
    installation_id: Annotated[str | None, Query(max_length=32)] = None,
    setup_action: Annotated[str | None, Query(max_length=32)] = None,
) -> RedirectResponse:
    """GitHub's Setup URL: the browser lands here after installing or changing the app.

    The installation id is only a claim at this point. We remember it in a signed cookie
    and send the user through a GitHub sign-in that proves they can access it; the shared
    callback then links it (see `finish_verification`).
    """
    if user is None:
        return _to_repos_error("not_signed_in", settings)
    if not installation_id:
        # e.g. an organisation owner still has to approve the installation.
        code = "installation_pending" if setup_action == "request" else "invalid_request"
        return _to_repos_error(code, settings)
    if not installation_id.isdecimal() or len(installation_id) > 18:
        return _to_repos_error("invalid_request", settings)

    start = repos.start_verification(user, int(installation_id))
    response = RedirectResponse(start.url, status_code=status.HTTP_302_FOUND)
    set_state_cookie(response, settings, start.state_cookie)
    return response


@router.get("/available", response_model=list[AvailableInstallationOut])
def available(user: CurrentUser, repos: RepoServiceDep) -> list[AvailableInstallationOut]:
    """Repositories the app can see, per installation, that can be connected."""
    return [
        AvailableInstallationOut(
            id=item.installation.id,
            account_login=item.installation.account_login,
            account_type=item.installation.account_type,
            reachable=item.reachable,
            repos=[AvailableRepoOut(full_name=r.full_name, private=r.private) for r in item.repos],
        )
        for item in repos.list_available(user)
    ]


@router.get("", response_model=list[RepoOut])
def list_repos(user: CurrentUser, repos: RepoServiceDep) -> list[RepoConnection]:
    return repos.list_connected(user)


@router.post("", response_model=RepoOut, status_code=status.HTTP_201_CREATED)
def connect_repo(body: RepoConnect, user: CurrentUser, repos: RepoServiceDep) -> RepoConnection:
    return repos.connect(user, body.installation_id, body.full_name)


@router.delete("/{repo_id}", status_code=status.HTTP_204_NO_CONTENT)
def disconnect_repo(repo_id: UUID, user: CurrentUser, repos: RepoServiceDep) -> Response:
    repos.disconnect(user, repo_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def is_verification_state(request: Request, settings: SettingsDep) -> bool:
    """True when the OAuth state cookie belongs to the installation check (not sign-in)."""
    value = request.cookies.get(STATE_COOKIE_NAME)
    if not value:
        return False
    codec = SignedCookieCodec(settings.secret_key.get_secret_value(), OAUTH_STATE_SALT)
    try:
        return codec.loads(value, settings.oauth_state_ttl_seconds).get("purpose") == VERIFY_PURPOSE
    except OAuthStateError:
        return False


def finish_verification(
    *,
    request: Request,
    user: User | None,
    repos: RepoServiceDep,
    settings: SettingsDep,
    code: str | None,
    state: str | None,
    error: str | None,
) -> RedirectResponse:
    """The shared GitHub callback, for the installation check. Always ends at /repos,
    with an error code on failure. Details stay in the logs."""
    failure: str | None = None
    next_path = DEFAULT_NEXT
    if error == "access_denied":
        failure = "access_denied"
    elif user is None:
        failure = "not_signed_in"
    elif error or not code or not state:
        failure = "invalid_request"
    else:
        try:
            repos.complete_verification(
                user=user,
                code=code,
                state=state,
                state_cookie=request.cookies.get(STATE_COOKIE_NAME),
            )
        except OAuthStateError:
            logger.warning("installation check rejected: invalid state")
            failure = "invalid_state"
        except InstallationVerificationError as exc:
            logger.warning("installation check rejected: %s", exc)
            failure = "installation_not_verified"
        except GitHubError as exc:
            logger.error("GitHub installation check failed: %s", exc)
            failure = "github_error"

    if failure is None:
        target = f"{settings.frontend_origin}{next_path}"
    else:
        target = f"{settings.frontend_origin}/repos?{urlencode({'error': failure})}"
    response = RedirectResponse(target, status_code=status.HTTP_302_FOUND)
    clear_state_cookie(response, settings)
    return response


def _to_repos_error(code: str, settings: SettingsDep) -> RedirectResponse:
    target = f"{settings.frontend_origin}/repos?{urlencode({'error': code})}"
    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)
