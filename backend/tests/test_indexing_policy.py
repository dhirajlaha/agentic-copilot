import pytest

from app.indexing import policy
from app.indexing.policy import SkipReason


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("src/app/main.py", None),
        ("README.md", None),
        (".env.example", None),  # a template meant to be committed
        (".env.sample", None),
        ("node_modules/left-pad/index.js", SkipReason.VENDORED),
        ("web/dist/bundle.js", SkipReason.VENDORED),
        ("a/b/__pycache__/x.pyc", SkipReason.VENDORED),
        ("uv.lock", SkipReason.LOCKFILE),
        ("frontend/package-lock.json", SkipReason.LOCKFILE),
        ("static/app.min.js", SkipReason.MINIFIED),
        ("static/app.js.map", SkipReason.MINIFIED),
        ("assets/logo.PNG", SkipReason.BINARY),
        ("docs/manual.pdf", SkipReason.BINARY),
        # Secrets
        (".env", SkipReason.SECRET_FILE),
        (".env.production", SkipReason.SECRET_FILE),
        ("config/.env.local", SkipReason.SECRET_FILE),
        ("deploy/server.pem", SkipReason.SECRET_FILE),
        ("keys/signing.key", SkipReason.SECRET_FILE),
        ("id_rsa", SkipReason.SECRET_FILE),
        (".npmrc", SkipReason.SECRET_FILE),
        ("secrets/db.txt", SkipReason.SECRET_FILE),
        (".aws/credentials", SkipReason.SECRET_FILE),
        ("infra/prod.tfvars", SkipReason.SECRET_FILE),
        ("terraform.tfstate", SkipReason.SECRET_FILE),
        ("CONFIG/CREDENTIALS.JSON", SkipReason.SECRET_FILE),  # case-insensitive
        # A credential inside a vendored directory is still reported as a credential.
        ("vendor/x/.env", SkipReason.SECRET_FILE),
    ],
)
def test_paths(path, reason):
    assert policy.skip_reason_for_path(path) == reason


def test_size_limits():
    assert policy.skip_reason_for_size(0) == SkipReason.EMPTY
    assert policy.skip_reason_for_size(10) is None
    assert policy.skip_reason_for_size(policy.MAX_FILE_BYTES) is None
    assert policy.skip_reason_for_size(policy.MAX_FILE_BYTES + 1) == SkipReason.TOO_LARGE


def test_decoding():
    assert policy.decode_text("héllo".encode()) == "héllo"
    assert policy.decode_text(b"abc\x00def") is None  # NUL byte: binary
    assert policy.decode_text(b"\xff\xfe\x00\x01") is None


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE...",
        "-----BEGIN PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "aws_key = 'AKIAIOSFODNN7EXAMPLE'",
        "token: ghp_" + "a" * 36,
        "github_pat_" + "A1b2" * 15,
        "SLACK=xoxb-123456789012-abcdefghij",
        "key = AIza" + "x" * 35,
        "ANTHROPIC=sk-ant-api03-" + "z" * 30,
        "OPENAI=sk-" + "q" * 40,
        "STRIPE=sk_live_" + "9" * 24,
    ],
)
def test_secret_shapes_are_detected(text):
    assert policy.contains_secret(f"# config\n{text}\nrest")


@pytest.mark.parametrize(
    "text",
    [
        "def add(a, b):\n    return a + b",
        "password = get_password_from_vault()",  # a name, not a value
        "skip_this = 'sk-short'",
        "ghp_ is a prefix we mention in docs",
        "-----BEGIN CERTIFICATE-----",  # public certificates are not secret
    ],
)
def test_ordinary_code_is_not_flagged(text):
    assert not policy.contains_secret(text)
