import base64
import hashlib
from typing import Any

from app.core.exceptions import GitHubError
from app.integrations.github import GitHubUser
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
