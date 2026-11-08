"""Turns the Coder's proposed edits into a checked patch.

The model's output is a proposal, never a fact. Every path is checked against the same
policy that keeps secrets out of the index; every `search` must match the real file exactly
once; created files must not already exist; the whole patch has size limits. Problems are
returned as readable messages so the Coder can be asked to fix them, and nothing is applied
unless the whole patch is valid.
"""

import difflib
from collections.abc import Callable
from dataclasses import dataclass

from app.agents.schemas import AppliedChange, CoderOutput, EditOut, FileChangeOut, Patch
from app.core.exceptions import AppError
from app.indexing import policy

MAX_CHANGED_FILES = 10
MAX_CHANGED_LINES = 1500  # added + removed, across the whole patch
MAX_PATH_LENGTH = 300

# Paths the app must not write even though the index may read them: a push that touches
# workflow files is rejected by GitHub unless the app has the `workflows` permission, and
# CI definitions are a privileged place for a bot to change.
_PROTECTED_PREFIXES = (".git/", ".github/workflows/")


@dataclass(frozen=True, slots=True)
class Problem:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def normalise_path(path: str) -> str:
    return path.strip().removeprefix("./").lstrip("/")


def path_problem(path: str) -> str | None:
    """Why the app must not write `path`, or None if it may."""
    if not path or len(path) > MAX_PATH_LENGTH:
        return "the path is empty or too long"
    if "\\" in path or any(ord(c) < 32 for c in path) or any(c in path for c in "<>"):
        return "the path contains characters that are not allowed"
    if any(part in ("", ".", "..") for part in path.split("/")):
        return "the path must be relative and must not contain '..' or empty parts"
    if path.startswith(_PROTECTED_PREFIXES):
        return "files under .git/ and .github/workflows/ cannot be changed by this tool"
    reason = policy.skip_reason_for_path(path)
    if reason is not None:
        return f"files of this kind are never changed ({reason.value.replace('_', ' ')})"
    return None


def apply_edits(original: str, edits: list[EditOut]) -> str:
    """Apply replacements in order, each to the text produced by the previous one.

    Raises `ValueError` with a message the model can act on.
    """
    text = original
    for number, edit in enumerate(edits, start=1):
        label = f"edit {number}"
        if not edit.search:
            raise ValueError(f"{label}: 'search' is empty")
        if edit.search == edit.replace:
            raise ValueError(f"{label}: 'search' and 'replace' are identical")
        count = text.count(edit.search)
        if count == 0:
            raise ValueError(
                f"{label}: 'search' text was not found in the file. Copy it exactly, "
                "including indentation and line breaks."
            )
        if count > 1:
            raise ValueError(
                f"{label}: 'search' text appears {count} times. Include enough surrounding "
                "lines to make it unique."
            )
        text = text.replace(edit.search, edit.replace, 1)
    return text


def unified_diff(path: str, before: str, after: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}" if before else "/dev/null",
        tofile=f"b/{path}" if after else "/dev/null",
    )
    # A line without a trailing newline would run into the next diff line.
    return "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines
    )


def _count_changes(before: str, after: str) -> tuple[int, int]:
    added = removed = 0
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def _check_change(
    change: FileChangeOut, read_file: Callable[[str], str | None]
) -> tuple[AppliedChange | None, list[Problem]]:
    path = normalise_path(change.path)
    if (message := path_problem(path)) is not None:
        return None, [Problem(change.path or "(empty path)", message)]
    try:
        existing = read_file(path)
    except AppError as exc:  # e.g. too large to read, or GitHub unreachable
        return None, [Problem(path, str(exc))]

    if change.action == "create":
        if existing is not None:
            return None, [Problem(path, "cannot create: the file already exists (use 'modify')")]
        if not change.content.strip():
            return None, [Problem(path, "cannot create an empty file")]
        if len(change.content.encode()) > policy.MAX_FILE_BYTES:
            return None, [Problem(path, "the new file is too large")]
        if policy.contains_secret(change.content):
            return None, [Problem(path, "the new file contains something shaped like a credential")]
        before, after, edits = "", change.content, []
    elif change.action == "delete":
        if existing is None:
            return None, [Problem(path, "cannot delete: the file does not exist")]
        before, after, edits = existing, "", []
    else:  # modify
        if existing is None:
            return None, [Problem(path, "cannot modify: the file does not exist (use 'create')")]
        if not change.edits:
            return None, [Problem(path, "'modify' needs at least one edit")]
        try:
            after = apply_edits(existing, change.edits)
        except ValueError as exc:
            return None, [Problem(path, str(exc))]
        if policy.contains_secret(after) and not policy.contains_secret(existing):
            return None, [Problem(path, "the change would add something shaped like a credential")]
        before, edits = existing, change.edits

    added, removed = _count_changes(before, after)
    return AppliedChange(
        path=path,
        action=change.action,
        reason=change.reason,
        edits=edits,
        content=change.content if change.action == "create" else "",
        diff=unified_diff(path, before, after),
        lines_added=added,
        lines_removed=removed,
    ), []


def build_patch(
    output: CoderOutput,
    *,
    base_commit_sha: str,
    read_file: Callable[[str], str | None],
    attempts: int,
) -> tuple[Patch | None, list[Problem]]:
    """Check every change. Returns the patch, or None plus every problem found (all of
    them, so one repair round can fix them together)."""
    problems: list[Problem] = []
    if len(output.changes) > MAX_CHANGED_FILES:
        problems.append(
            Problem(
                "(patch)",
                f"too many files ({len(output.changes)}); the limit is {MAX_CHANGED_FILES}",
            )
        )

    applied: list[AppliedChange] = []
    seen: set[str] = set()
    for change in output.changes[:MAX_CHANGED_FILES]:
        path = normalise_path(change.path)
        if path in seen:
            problems.append(
                Problem(path, "appears more than once; combine its edits into one entry")
            )
            continue
        seen.add(path)
        result, found = _check_change(change, read_file)
        problems += found
        if result is not None:
            applied.append(result)

    total = sum(c.lines_added + c.lines_removed for c in applied)
    if total > MAX_CHANGED_LINES:
        problems.append(
            Problem("(patch)", f"{total} changed lines is over the limit of {MAX_CHANGED_LINES}")
        )
    if problems:
        return None, problems
    return Patch(
        base_commit_sha=base_commit_sha,
        summary=output.summary.strip(),
        changes=applied,
        open_questions=output.open_questions,
        attempts=attempts,
        files_changed=len(applied),
        lines_added=sum(c.lines_added for c in applied),
        lines_removed=sum(c.lines_removed for c in applied),
    ), []
