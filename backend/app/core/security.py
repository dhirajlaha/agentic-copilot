"""Token, hashing and signed-cookie helpers. Wraps well-tested primitives; nothing here
invents cryptography."""

import base64
import hashlib
import secrets
from typing import Any

from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.core.exceptions import OAuthStateError


def generate_token() -> str:
    """256 bits of randomness, URL-safe. Used for session cookies and OAuth state."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 hex digest. Fast hashing is fine because tokens are high-entropy random
    values (not passwords), so there is nothing to brute-force."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE with the S256 method (RFC 7636).

    The challenge goes to GitHub at the start; the verifier is revealed only when the
    code is exchanged, so a stolen authorization code is useless on its own.
    """
    verifier = secrets.token_urlsafe(64)  # 86 chars, within the 43-128 allowed
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def safe_next_path(path: str | None) -> str:
    """Only allow same-site relative paths, so `?next=` cannot become an open redirect."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    if any(ord(c) < 32 for c in path):
        return "/"
    return path


class SignedCookieCodec:
    """Signs and time-limits small JSON payloads stored in cookies."""

    def __init__(self, secret_key: str, salt: str) -> None:
        self._serializer = URLSafeTimedSerializer(secret_key, salt=salt)

    def dumps(self, payload: dict[str, Any]) -> str:
        return self._serializer.dumps(payload)

    def loads(self, value: str, max_age_seconds: int) -> dict[str, Any]:
        try:
            payload = self._serializer.loads(value, max_age=max_age_seconds)
        except BadSignature as exc:  # includes SignatureExpired
            raise OAuthStateError("Invalid or expired state") from exc
        if not isinstance(payload, dict):
            raise OAuthStateError("Invalid state payload")
        return payload
