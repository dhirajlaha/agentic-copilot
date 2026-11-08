"""Repository connection routes. Thin: parse, call RepoService, shape the response."""

import logging
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.api.cookies import STATE_COOKIE_NAME, clear_state_cookie, set_state_cookie
from app.api.deps import CurrentUser, RepoServiceDep, SettingsDep
from app.core.exceptions import (
    GitHubError,
    InstallationVerificationError,
    OAuthStateError,
)
from app.db.models import RepoConnection, User
from app.schemas.repo import (
    AvailableInstallationOut,
    AvailableRepoOut,
    RepoConnect,
    RepoOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/repos", tags=["repos"])


@router.get("/install", status_code=status.HTTP_302_FOUND)
def install(
    user: CurrentUser,
    repos: RepoServiceDep,
    settings: SettingsDep,
    next_path: Annotated[str | None, Query(alias="next", max_length=2048)] = None,
) -> RedirectResponse:
    """Send the browser to GitHub to install the app (and choose repositories)."""
    start = repos.start_install(user, next_path)
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


def finish_install_redirect(
    *,
    request: Request,
    user: User | None,
    repos: RepoServiceDep,
    settings: SettingsDep,
    code: str | None,
    state: str | None,
    installation_id: str | None,
    error: str | None,
) -> RedirectResponse:
    """Handle GitHub's redirect after installing the app (it arrives on the shared
    /auth/github/callback). Every outcome is a redirect to the frontend; details of a
    failure stay in the logs."""
    failure: str | None = None
    next_path = "/repos"
    if error == "access_denied":
        failure = "access_denied"
    elif user is None:
        failure = "not_signed_in"
    elif error or not code or not state:
        failure = "invalid_request"
    elif not installation_id:
        # e.g. an organisation owner must approve the installation first.
        failure = "installation_pending"
    elif not installation_id.isdecimal() or len(installation_id) > 18:
        failure = "invalid_request"
    else:
        try:
            next_path = repos.complete_install(
                user=user,
                code=code,
                state=state,
                state_cookie=request.cookies.get(STATE_COOKIE_NAME),
                installation_id=int(installation_id),
            )
        except OAuthStateError:
            logger.warning("install callback rejected: invalid state")
            failure = "invalid_state"
        except InstallationVerificationError as exc:
            logger.warning("install callback rejected: %s", exc)
            failure = "installation_not_verified"
        except GitHubError as exc:
            logger.error("GitHub install verification failed: %s", exc)
            failure = "github_error"

    if failure is None:
        target = f"{settings.frontend_origin}{next_path}"
    else:
        target = f"{settings.frontend_origin}/repos?{urlencode({'error': failure})}"
    response = RedirectResponse(target, status_code=status.HTTP_302_FOUND)
    clear_state_cookie(response, settings)
    return response
