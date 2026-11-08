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

    def fetch_installations(self, code: str, code_verifier: str) -> InstallationGrant:
        self.codes_seen.append(code)
        if self.error is not None:
            raise self.error
        digest = hashlib.sha256(code_verifier.encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != self.challenges[-1]:
            raise GitHubError("PKCE verification failed")
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

    def generate_structured(
        self, *, system: str, prompt: str, schema: type, thinking: bool = False
    ) -> LLMResult[Any]:
        self.calls.append(
            {"system": system, "prompt": prompt, "schema": schema, "thinking": thinking}
        )
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

    def install_url(self) -> str:
        return "https://github.test/apps/test-app/installations/new"

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


class FakeEmbedder:
    """An EmbeddingClient that hashes words into a fixed-size vector, so texts sharing
    words are closer than texts that do not (enough to test search and re-indexing)."""

    provider = "fake"
    model = "fake-embedding"

    def __init__(self, dimensions: int = 768, batch_size: int = 4) -> None:
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.error: Exception | None = None
        self.batches: list[list[str]] = []  # every embed_documents call, for assertions

    @property
    def texts_embedded(self) -> int:
        return sum(len(batch) for batch in self.batches)

    def _vector(self, text: str) -> list[float]:
        import math
        import re
        import zlib

        vector = [0.0] * self.dimensions
        for word in re.findall(r"[a-z0-9_]+", text.lower()):
            vector[zlib.crc32(word.encode()) % self.dimensions] += 1.0
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        return [x / norm for x in vector]

    def embed_documents(self, texts):
        if self.error is not None:
            raise self.error
        self.batches.append(list(texts))
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        return self._vector(text)


class FakeSnapshots:
    """A SnapshotProvider over an in-memory repository: {path: text}."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = dict(files or {})
        self.commit = "a" * 40
        self.skipped: dict[str, int] = {}
        self.secret_paths: list[str] = []
        self.head_calls = 0
        self.snapshot_calls = 0

    def head_commit(self, repo_full_name: str, token: str) -> str:
        self.head_calls += 1
        return self.commit

    def snapshot(self, repo_full_name: str, token: str, commit_sha: str):
        from collections import Counter

        from app.indexing.snapshot import Snapshot, SnapshotFile, git_blob_sha

        self.snapshot_calls += 1
        snapshot = Snapshot(commit_sha=commit_sha)
        for path, text in self.files.items():
            snapshot.files[path] = SnapshotFile(path, git_blob_sha(text.encode()), text)
        snapshot.skipped = Counter(self.skipped)
        snapshot.secret_paths = list(self.secret_paths)
        return snapshot


class FakeIndexer:
    """Stands in for RepoIndexer inside executor tests."""

    embedding_model = "fake-embedding"

    def __init__(self, error: Exception | None = None) -> None:
        from app.services.index_service import IndexResult

        self.error = error
        self.result = IndexResult(
            commit_sha="b" * 40, up_to_date=False, files_total=3, files_indexed=3, chunks_written=5
        )
        self.calls: list[dict] = []

    def sync(self, *, repo_connection_id, repo_full_name, token, heartbeat):
        self.calls.append({"repo": repo_full_name, "token": token})
        heartbeat()
        if self.error is not None:
            raise self.error
        return self.result
