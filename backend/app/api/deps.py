"""FastAPI wiring: builds services from settings and the request. Routers depend on
these, so tests can override any single piece with `app.dependency_overrides`."""

from typing import Annotated

import httpx
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import GitHubAppSettings, Settings, get_github_app_settings, get_settings
from app.core.exceptions import AuthenticationError
from app.core.security import SignedCookieCodec
from app.db.models import User
from app.db.session import get_session
from app.integrations.github import GitHubOAuth, GitHubOAuthClient
from app.integrations.github_app import GitHubApp, GitHubAppClient
from app.integrations.github_files import GitHubFileReader
from app.integrations.github_publish import GitHubPullRequestPublisher
from app.repositories.github_installation_repository import GitHubInstallationRepository
from app.repositories.repo_connection_repository import RepoConnectionRepository
from app.repositories.run_repository import AgentRunRepository
from app.repositories.session_repository import UserSessionRepository
from app.repositories.ticket_repository import TicketRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.services.publish_service import PublishService
from app.services.repo_service import RepoService
from app.services.run_service import RunService
from app.services.ticket_service import TicketService

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

OAUTH_STATE_SALT = "github-oauth-state"


def get_http_client(request: Request) -> httpx.Client:
    """The shared outbound HTTP client created in the app lifespan."""
    return request.app.state.http_client


def get_github_oauth(
    settings: SettingsDep, http: Annotated[httpx.Client, Depends(get_http_client)]
) -> GitHubOAuth:
    return GitHubOAuthClient(
        client_id=settings.github_client_id,
        client_secret=settings.github_client_secret.get_secret_value(),
        redirect_uri=settings.github_redirect_uri,
        http=http,
    )


def get_auth_service(
    session: SessionDep,
    settings: SettingsDep,
    github: Annotated[GitHubOAuth, Depends(get_github_oauth)],
) -> AuthService:
    return AuthService(
        session=session,
        users=UserRepository(session),
        sessions=UserSessionRepository(session),
        github=github,
        state_codec=SignedCookieCodec(settings.secret_key.get_secret_value(), OAUTH_STATE_SALT),
        session_ttl_seconds=settings.session_ttl_seconds,
        oauth_state_ttl_seconds=settings.oauth_state_ttl_seconds,
    )


def get_current_user(
    request: Request,
    settings: SettingsDep,
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> User:
    return auth.authenticate(request.cookies.get(settings.session_cookie_name))


def get_optional_user(
    request: Request,
    settings: SettingsDep,
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> User | None:
    """The signed-in user, or None. For browser redirects that must end in a redirect
    (not a JSON 401) when the visitor is not signed in."""
    try:
        return auth.authenticate(request.cookies.get(settings.session_cookie_name))
    except AuthenticationError:
        return None


def get_github_app(
    http: Annotated[httpx.Client, Depends(get_http_client)],
    app_settings: Annotated[GitHubAppSettings, Depends(get_github_app_settings)],
) -> GitHubApp:
    return GitHubAppClient(
        client_id=app_settings.github_client_id,
        private_key_pem=app_settings.private_key_pem.get_secret_value(),
        slug=app_settings.github_app_slug,
        http=http,
    )


def get_repo_service(
    session: SessionDep,
    settings: SettingsDep,
    oauth: Annotated[GitHubOAuth, Depends(get_github_oauth)],
    github_app: Annotated[GitHubApp, Depends(get_github_app)],
) -> RepoService:
    return RepoService(
        session=session,
        installations=GitHubInstallationRepository(session),
        repos=RepoConnectionRepository(session),
        oauth=oauth,
        app=github_app,
        state_codec=SignedCookieCodec(settings.secret_key.get_secret_value(), OAUTH_STATE_SALT),
        state_ttl_seconds=settings.oauth_state_ttl_seconds,
    )


def get_ticket_service(session: SessionDep) -> TicketService:
    return TicketService(session, TicketRepository(session), RepoConnectionRepository(session))


def get_publish_service(
    session: SessionDep,
    github_app: Annotated[GitHubApp, Depends(get_github_app)],
    http: Annotated[httpx.Client, Depends(get_http_client)],
) -> PublishService:
    return PublishService(
        session=session,
        runs=AgentRunRepository(session),
        tokens=github_app,
        files=GitHubFileReader(http),
        publisher=GitHubPullRequestPublisher(http),
    )


def get_run_service(session: SessionDep) -> RunService:
    return RunService(session, TicketRepository(session), AgentRunRepository(session))


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
TicketServiceDep = Annotated[TicketService, Depends(get_ticket_service)]
RunServiceDep = Annotated[RunService, Depends(get_run_service)]
PublishServiceDep = Annotated[PublishService, Depends(get_publish_service)]
RepoServiceDep = Annotated[RepoService, Depends(get_repo_service)]
CurrentUser = Annotated[User, Depends(get_current_user)]
UserOrNone = Annotated[User | None, Depends(get_optional_user)]
