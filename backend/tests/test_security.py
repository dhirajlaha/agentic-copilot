import base64
import hashlib
import time

import pytest

from app.core.exceptions import OAuthStateError
from app.core.security import (
    SignedCookieCodec,
    generate_pkce_pair,
    generate_token,
    hash_token,
    safe_next_path,
)


def test_tokens_are_unique_and_hash_is_stable():
    assert generate_token() != generate_token()
    assert hash_token("abc") == hash_token("abc")
    assert hash_token("abc") != hash_token("abd")
    assert len(hash_token("abc")) == 64


@pytest.mark.parametrize("path", ["/", "/dashboard", "/tickets?x=1"])
def test_safe_next_path_allows_relative(path):
    assert safe_next_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        None,
        "",
        "dashboard",
        "//evil.com",
        "https://evil.com",
        "/\\evil.com",
        "/a\nb",
        "javascript:alert(1)",
    ],
)
def test_safe_next_path_rejects_open_redirects(path):
    assert safe_next_path(path) == "/"


def test_codec_round_trip_tamper_and_expiry(monkeypatch):
    codec = SignedCookieCodec("k" * 32, salt="s")
    value = codec.dumps({"a": 1})
    assert codec.loads(value, 60) == {"a": 1}
    with pytest.raises(OAuthStateError):
        codec.loads(value + "x", 60)
    with pytest.raises(OAuthStateError):
        SignedCookieCodec("k" * 32, salt="other").loads(value, 60)
    # Move the clock forward past the allowed age instead of sleeping.
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 120)
    with pytest.raises(OAuthStateError):
        codec.loads(value, 60)


def test_pkce_pair_matches_rfc7636_s256():
    verifier, challenge = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode()
    assert "=" not in challenge
    assert generate_pkce_pair()[0] != verifier
