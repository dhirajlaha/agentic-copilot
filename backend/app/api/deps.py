"""FastAPI wiring: builds services from settings and the request. Routers depend on
these, so tests can override any single piece with `app.dependency_overrides`."""

from typing import Annotated

import httpx
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import SignedCookieCodec
from app.db.models import User
from app.db.session import get_session
from app.integrations.github import GitHubOAuth, GitHubOAuthClient
from app.repositories.repo_connection_repository import RepoConnectionRepository
from app.repositories.session_repository import UserSessionRepository
from app.repositories.ticket_repository import TicketRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
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


def get_ticket_service(session: SessionDep) -> TicketService:
    return TicketService(session, TicketRepository(session), RepoConnectionRepository(session))


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
TicketServiceDep = Annotated[TicketService, Depends(get_ticket_service)]
CurrentUser = Annotated[User, Depends(get_current_user)]
