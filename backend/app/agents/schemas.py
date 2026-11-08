"""Data models exchanged by the agents (LLM output schemas and verified results)."""

from typing import Literal

from pydantic import BaseModel


# --- What the LLM must return (kept simple: no defaults or constraints, because
# --- providers' structured-output modes support only a subset of JSON Schema).
class SubtaskOut(BaseModel):
    title: str
    details: str


class CandidateFileOut(BaseModel):
    path: str
    reason: str


class PlannerOutput(BaseModel):
    subtasks: list[SubtaskOut]
    candidate_files: list[CandidateFileOut]
    open_questions: list[str]


# --- The verified plan the rest of the pipeline uses.
class Plan(BaseModel):
    subtasks: list[SubtaskOut]
    candidate_files: list[CandidateFileOut]
    open_questions: list[str]
    # Paths the model proposed that are not in the repository (hallucinations). Kept for
    # observability and evaluation, never used downstream.
    rejected_files: list[str]


# --- Coder -------------------------------------------------------------------------------
# What the LLM must return. An edit is an exact-text replacement, not a line number or a
# diff: models copy text far more reliably than they count lines, and a replacement that
# does not match the file can be detected and repaired instead of corrupting it.
class EditOut(BaseModel):
    search: str  # exact text from the file; must occur exactly once
    replace: str


class FileChangeOut(BaseModel):
    path: str
    action: Literal["modify", "create", "delete"]
    edits: list[EditOut]  # used for "modify"; empty otherwise
    content: str  # the whole new file for "create"; empty otherwise
    reason: str


class CoderOutput(BaseModel):
    summary: str
    changes: list[FileChangeOut]
    open_questions: list[str]


# The verified patch the rest of the pipeline uses.
class AppliedChange(BaseModel):
    path: str
    action: Literal["modify", "create", "delete"]
    reason: str
    # Kept so the change can be re-applied to the same base commit when publishing.
    edits: list[EditOut]
    content: str
    diff: str  # unified diff, for review
    lines_added: int
    lines_removed: int


class Patch(BaseModel):
    base_commit_sha: str  # the commit every change was checked against
    summary: str
    changes: list[AppliedChange]
    open_questions: list[str]
    attempts: int  # 1 = the model got it right first time
    files_changed: int
    lines_added: int
    lines_removed: int
