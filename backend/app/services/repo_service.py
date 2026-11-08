"""Connecting GitHub repositories: install the GitHub App, prove the installation belongs
to the signed-in user, and choose which repositories to work on."""

import logging
import secrets
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.exceptions import (
    ConflictError,
    InstallationVerificationError,
    NotFoundError,
    OAuthStateError,
    RepositoryAccessError,
)
from app.core.security import SignedCookieCodec, generate_pkce_pair, generate_token, safe_next_path
from app.db.models import GitHubInstallation, RepoConnection, User
from app.integrations.github import GitHubOAuth
from app.integrations.github_app import GitHubApp, InstallationRepo
from app.integrations.github_repos import REPO_NAME
from app.repositories.github_installation_repository import GitHubInstallationRepository
from app.repositories.repo_connection_repository import RepoConnectionRepository

logger = logging.getLogger(__name__)

VERIFY_PURPOSE = "verify_install"
DEFAULT_NEXT = "/repos"


def _return_path(path: object) -> str:
    """The page to land on afterwards: a safe same-site path, else the repositories page."""
    if isinstance(path, str) and path != "/" and safe_next_path(path) == path:
        return path
    return DEFAULT_NEXT


@dataclass(frozen=True, slots=True)
class VerifyStart:
    url: str  # GitHub sign-in that proves the user can access the installation
    state_cookie: str


@dataclass(frozen=True, slots=True)
class AvailableInstallation:
    installation: GitHubInstallation
    repos: list[InstallationRepo]
    reachable: bool  # False when GitHub says the app no longer has access


class RepoService:
    def __init__(
        self,
        *,
        session: Session,
        installations: GitHubInstallationRepository,
        repos: RepoConnectionRepository,
        oauth: GitHubOAuth,
        app: GitHubApp,
        state_codec: SignedCookieCodec,
        state_ttl_seconds: int,
    ) -> None:
        self._session = session
        self._installations = installations
        self._repos = repos
        self._oauth = oauth
        self._app = app
        self._state_codec = state_codec
        self._state_ttl_seconds = state_ttl_seconds

    def install_url(self) -> str:
        """Where to send the user to install the app or change its repositories."""
        return self._app.install_url()

    def start_verification(self, user: User, installation_id: int) -> VerifyStart:
        """Step 2 of connecting an installation: prove this user can access it.

        GitHub sends the browser back to us with an `installation_id` in the URL, which
        anyone can edit. So we only remember the id here, in a signed cookie bound to the
        user, and send them through a normal GitHub sign-in (PKCE, as for login). The
        callback then checks the id against what GitHub says the user can access.
        """
        state = generate_token()
        verifier, challenge = generate_pkce_pair()
        cookie = self._state_codec.dumps(
            {
                "purpose": VERIFY_PURPOSE,
                "state": state,
                "verifier": verifier,
                "uid": str(user.id),
                "installation_id": installation_id,
            }
        )
        return VerifyStart(self._oauth.authorization_url(state, challenge), cookie)

    def complete_verification(
        self, *, user: User, code: str, state: str, state_cookie: str | None
    ) -> None:
        """Finish step 2: confirm the installation belongs to this user, then link it."""
        if not state_cookie:
            raise OAuthStateError("Missing state cookie")
        payload = self._state_codec.loads(state_cookie, self._state_ttl_seconds)
        expected = payload.get("state")
        if payload.get("purpose") != VERIFY_PURPOSE:
            raise OAuthStateError("State was issued for a different flow")
        if not isinstance(expected, str) or not secrets.compare_digest(expected, state):
            raise OAuthStateError("State mismatch")
        if payload.get("uid") != str(user.id):
            raise OAuthStateError("State belongs to a different user")
        verifier = payload.get("verifier")
        installation_id = payload.get("installation_id")
        if not isinstance(verifier, str) or not isinstance(installation_id, int):
            raise OAuthStateError("Malformed verification state")

        grant = self._oauth.fetch_installations(code, verifier)
        if grant.github_user_id != user.github_id:
            raise InstallationVerificationError("Authorised GitHub account does not match")
        match = next((i for i in grant.installations if i.id == installation_id), None)
        if match is None:
            raise InstallationVerificationError("Installation is not accessible to this user")

        self._installations.upsert(
            user_id=user.id,
            installation_id=match.id,
            account_login=match.account_login,
            account_type=match.account_type,
        )
        self._session.commit()
        logger.info("installation linked", extra={"user_id": str(user.id)})

    def list_available(self, user: User) -> list[AvailableInstallation]:
        result = []
        for installation in self._installations.list_for_user(user.id):
            try:
                repos = self._app.list_repositories(installation.installation_id)
            except RepositoryAccessError:
                result.append(AvailableInstallation(installation, [], reachable=False))
            else:
                result.append(AvailableInstallation(installation, repos, reachable=True))
        return result

    def connect(self, user: User, installation_pk: UUID, full_name: str) -> RepoConnection:
        installation = self._installations.get_owned(installation_pk, user.id)
        if installation is None:
            raise NotFoundError("Installation")
        if not REPO_NAME.match(full_name):
            raise RepositoryAccessError("Invalid repository name")
        # Ask GitHub, not the client: only repositories the installation can really see.
        available = self._app.list_repositories(installation.installation_id)
        wanted = full_name.lower()
        repo = next((r for r in available if r.full_name.lower() == wanted), None)
        if repo is None:
            raise RepositoryAccessError(
                f"{full_name} is not available to this installation. Add it to the app's "
                "repository access on GitHub first."
            )
        connection = self._repos.add(
            user_id=user.id,
            github_installation_id=installation.id,
            repo_full_name=repo.full_name,  # GitHub's canonical casing
        )
        if connection is None:
            raise ConflictError(f"{repo.full_name} is already connected")
        self._session.commit()
        return connection

    def list_connected(self, user: User) -> list[RepoConnection]:
        return self._repos.list_for_user(user.id)

    def disconnect(self, user: User, repo_id: UUID) -> None:
        repo = self._repos.get_owned(repo_id, user.id)
        if repo is None:
            raise NotFoundError("Repository")
        self._repos.delete(repo)
        self._session.commit()
