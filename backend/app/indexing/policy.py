"""What is allowed into the index.

Two goals. Keep the index useful (no vendored code, lockfiles, binaries or generated
bundles to drown real code). And never send secrets to the embedding provider: files that
look like credentials, or contain something shaped like one, are skipped whole. Only a
path and a reason are ever recorded for a skipped file, never its content.
"""

import enum
import re
from pathlib import PurePosixPath

MAX_FILE_BYTES = 256 * 1024


class SkipReason(enum.StrEnum):
    VENDORED = "vendored_or_generated"
    LOCKFILE = "lockfile"
    MINIFIED = "minified"
    BINARY = "binary"
    TOO_LARGE = "too_large"
    EMPTY = "empty"
    SECRET_FILE = "secret_file"  # noqa: S105  (a label, not a credential)
    POSSIBLE_SECRET = "possible_secret"  # noqa: S105


# Any path component equal to one of these means the file is dependencies or build output.
_VENDORED_DIRS = frozenset(
    {
        ".git", "node_modules", "vendor", "bower_components", "dist", "build", "out", "target",
        "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".ruff_cache", ".pytest_cache",
        ".next", ".nuxt", ".gradle", ".idea", "coverage", "site-packages", ".terraform",
    }
)  # fmt: skip

_LOCKFILES = frozenset(
    {
        "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock",
        "Pipfile.lock", "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum",
        "bun.lockb", "mix.lock", "pubspec.lock", "Podfile.lock",
    }
)  # fmt: skip

_BINARY_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".psd", ".svgz",
        ".pdf", ".zip", ".gz", ".tar", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war",
        ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".mov", ".avi", ".wav",
        ".webm", ".ogg", ".flac", ".wasm", ".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe",
        ".bin", ".o", ".a", ".class", ".sqlite", ".db", ".parquet", ".onnx", ".pt", ".pb",
    }
)  # fmt: skip

_SECRET_DIRS = frozenset({"secrets", ".secrets", ".aws", ".ssh", ".gnupg", ".kube"})
_SECRET_NAMES = frozenset(
    {
        "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "credentials.json",
        ".npmrc", ".pypirc", ".netrc", ".htpasswd", "secrets.yml", "secrets.yaml",
        "secrets.json", "terraform.tfstate", "terraform.tfstate.backup",
    }
)  # fmt: skip
_SECRET_SUFFIXES = frozenset(
    {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk", ".tfvars", ".kdbx"}
)
# Templates that are meant to be committed and contain no real values.
_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template", ".env.dist"})

# High-confidence shapes of real credentials. Deliberately narrow: a false positive only
# costs one file of context, but a miss would leak a secret.
_SECRET_CONTENT = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),  # Slack
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),  # Google API key
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"),  # Anthropic
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}\b"),  # OpenAI-style
    re.compile(r"\b[sr]k_live_[0-9a-zA-Z]{24,}\b"),  # Stripe live keys
]  # fmt: skip


def skip_reason_for_path(path: str) -> SkipReason | None:
    """Decide from the path alone (cheap, before any content is read)."""
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    lowered = name.lower()
    directories = {part.lower() for part in parts[:-1]}

    # Secrets first, so a credential file inside a vendored directory is still reported
    # as one.
    if directories & _SECRET_DIRS or lowered in _SECRET_NAMES:
        return SkipReason.SECRET_FILE
    if lowered.startswith(".env") and lowered not in _ENV_TEMPLATES:
        return SkipReason.SECRET_FILE
    if PurePosixPath(lowered).suffix in _SECRET_SUFFIXES:
        return SkipReason.SECRET_FILE
    if directories & _VENDORED_DIRS:
        return SkipReason.VENDORED
    if name in _LOCKFILES:
        return SkipReason.LOCKFILE
    if lowered.endswith((".min.js", ".min.css", ".map")):
        return SkipReason.MINIFIED
    if PurePosixPath(lowered).suffix in _BINARY_SUFFIXES:
        return SkipReason.BINARY
    return None


def skip_reason_for_size(size: int) -> SkipReason | None:
    if size == 0:
        return SkipReason.EMPTY
    if size > MAX_FILE_BYTES:
        return SkipReason.TOO_LARGE
    return None


def decode_text(data: bytes) -> str | None:
    """The file as text, or None when it is binary (NUL bytes or not valid UTF-8)."""
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_CONTENT)
