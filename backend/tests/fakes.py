import base64
import hashlib
from typing import Any

from app.core.exceptions import GitHubError
from app.integrations.github import GitHubUser, InstallationGrant, InstallationInfo
from app.integrations.github_app import InstallationRepo
from app.llm.client import LLMResult, LLMUsage


class FakeGitHub:
    """Stands in for GitHub in API tests. Enforces PKCE like the real server does:
    the verifier presented at exchange must hash to the challenge sent at the start."""

    def __init__(self) -> None:
        self.user = GitHubUser(
            id=4242, login="octocat", email="octo@example.com", avatar_url="https://a/1.png"
        )
        self.error: Exception | None = None
        self.codes_seen: list[str] = []
        self.challenges: list[str] = []
        # The install flow: who authorised (defaults to `user`) and what they can access.
        self.grant_user_id: int | None = None
        self.installations: list[InstallationInfo] = []

    def fetch_installations(self, code: str) -> InstallationGrant:
        self.codes_seen.append(code)
        if self.error is not None:
            raise self.error
        user_id = self.grant_user_id if self.grant_user_id is not None else self.user.id
        return InstallationGrant(github_user_id=user_id, installations=list(self.installations))

    def authorization_url(self, state: str, code_challenge: str) -> str:
        self.challenges.append(code_challenge)
        return f"https://github.test/authorize?state={state}&code_challenge={code_challenge}"

    def fetch_user(self, code: str, code_verifier: str) -> GitHubUser:
        self.codes_seen.append(code)
        if self.error is not None:
            raise self.error
        digest = hashlib.sha256(code_verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if challenge != self.challenges[-1]:
            raise GitHubError("PKCE verification failed")
        return self.user


class FakeLLM:
    """An LLMClient that returns canned outputs and records every call."""

    provider = "fake"
    model = "fake-model"

    def __init__(self, output: Any = None, error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def generate_structured(self, *, system: str, prompt: str, schema: type) -> LLMResult[Any]:
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        if self.error is not None:
            raise self.error
        return LLMResult(
            output=self.output,
            usage=LLMUsage(input_tokens=120, output_tokens=30),
            model=self.model,
            latency_ms=42,
            request_id="req_test",
        )


class FakeRepoFiles:
    """RepoFilesProvider returning fixed paths, or raising a configured error."""

    def __init__(self, files: list[str] | None = None, error: Exception | None = None) -> None:
        self.files = files if files is not None else ["src/login.py", "tests/test_login.py"]
        self.error = error
        self.requested: list[str] = []
        self.tokens: list[str | None] = []

    def list_files(self, repo_full_name: str, token: str | None = None) -> list[str]:
        self.requested.append(repo_full_name)
        self.tokens.append(token)
        if self.error is not None:
            raise self.error
        return self.files


class FakeGitHubApp:
    """Stands in for the GitHub App client: canned repositories, recorded token requests."""

    def __init__(self) -> None:
        self.repos: dict[int, list[InstallationRepo]] = {}
        self.error: Exception | None = None
        self.unreachable: set[int] = set()
        self.tokens_requested: list[int] = []

    def install_url(self, state: str) -> str:
        return f"https://github.test/apps/test-app/installations/new?state={state}"

    def installation_token(self, installation_id: int) -> str:
        if self.error is not None:
            raise self.error
        self.tokens_requested.append(installation_id)
        return f"token-for-{installation_id}"

    def list_repositories(self, installation_id: int) -> list[InstallationRepo]:
        from app.core.exceptions import RepositoryAccessError

        if installation_id in self.unreachable:
            raise RepositoryAccessError("gone")
        if self.error is not None:
            raise self.error
        return self.repos.get(installation_id, [])
