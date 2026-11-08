"""A point-in-time copy of a repository's indexable text files."""

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol


def git_blob_sha(data: bytes) -> str:
    """The SHA-1 git itself assigns to a file's content ("blob"). Computing it ourselves
    means files can be compared across commits without asking GitHub for the tree."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324  (not security)


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    path: str
    sha: str
    text: str


@dataclass(slots=True)
class Snapshot:
    commit_sha: str
    files: dict[str, SnapshotFile] = field(default_factory=dict)
    skipped: Counter[str] = field(default_factory=Counter)  # reason -> number of files
    # Paths (never contents) of files left out because they look like credentials.
    secret_paths: list[str] = field(default_factory=list)


class SnapshotProvider(Protocol):
    def head_commit(self, repo_full_name: str, token: str) -> str:
        """SHA of the latest commit on the default branch."""
        ...

    def snapshot(self, repo_full_name: str, token: str, commit_sha: str) -> Snapshot:
        """All indexable text files as of `commit_sha`."""
        ...
