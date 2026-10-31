"""Data models exchanged by the agents (LLM output schemas and verified results)."""

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
