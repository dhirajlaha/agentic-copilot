"""Planner agent: turns a ticket into ordered sub-tasks and a first guess at which files
are involved, using only the repository's file paths (no code retrieval yet).

Flow: build a prompt (ticket as escaped, delimited data + the file list) -> ask the LLM
for structured output -> verify it against the real file list -> return a Plan.
"""

import json
import logging
from pathlib import Path
from typing import Any

from app.agents.schemas import CandidateFileOut, Plan, PlannerOutput
from app.agents.state import RunState
from app.agents.steps import step_from_llm_result
from app.llm.client import LLMClient

logger = logging.getLogger(__name__)

AGENT_NAME = "planner"
SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "planner_system.md").read_text()

MAX_FILES_IN_PROMPT = 3000  # keeps the prompt bounded on very large repositories
MAX_SUBTASKS = 8
MAX_CANDIDATE_FILES = 10


class PlanningError(Exception):
    """The model's answer could not be turned into a usable plan."""


def _is_safe_path(path: str) -> bool:
    # Angle brackets could close our prompt delimiters; control characters are never
    # legitimate in repository paths.
    return not any(c in path for c in "<>") and all(ord(c) >= 32 for c in path)


def _normalise(path: str) -> str:
    return path.strip().removeprefix("./").lstrip("/")


def build_user_prompt(state: RunState) -> tuple[str, int, int]:
    """Return (prompt, files_shown, files_omitted)."""
    ticket = json.dumps(
        {"title": state["title"], "description": state["description"]}, ensure_ascii=False
    )
    # JSON-escape "<" so ticket text can never produce a literal "</ticket>" and break out
    # of the data block (a prompt-injection technique). "<" is still valid JSON.
    ticket = ticket.replace("<", "\\u003c")

    files = sorted({_normalise(p) for p in state["repo_files"] if _is_safe_path(p)})
    shown = files[:MAX_FILES_IN_PROMPT]
    omitted = len(files) - len(shown)
    header = f'<repository_files shown="{len(shown)}" not_shown="{omitted}">'
    note = (
        f"\n({omitted} more paths exist but are not listed; prefer listed paths.)"
        if omitted
        else ""
    )
    prompt = (
        f"<ticket>\n{ticket}\n</ticket>\n\n{header}\n"
        + "\n".join(shown)
        + f"\n</repository_files>{note}\n\nProduce the plan."
    )
    return prompt, len(shown), omitted


def verify_plan(output: PlannerOutput, repo_files: list[str]) -> Plan:
    """Never trust model output blindly: keep only real paths, dedupe, apply limits."""
    if not output.subtasks:
        raise PlanningError("The model returned no sub-tasks")
    known = {_normalise(p) for p in repo_files}
    accepted: list[CandidateFileOut] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for candidate in output.candidate_files:
        path = _normalise(candidate.path)
        if path not in known:
            rejected.append(candidate.path)
            continue
        if path in seen:
            continue
        seen.add(path)
        accepted.append(CandidateFileOut(path=path, reason=candidate.reason))
    return Plan(
        subtasks=output.subtasks[:MAX_SUBTASKS],
        candidate_files=accepted[:MAX_CANDIDATE_FILES],
        open_questions=output.open_questions,
        rejected_files=rejected,
    )


class PlannerAgent:
    """A LangGraph node. Stateless apart from its injected LLM client."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def __call__(self, state: RunState) -> dict[str, Any]:
        prompt, shown, omitted = build_user_prompt(state)
        if omitted:
            logger.warning("planner: %d repository paths not shown to the model", omitted)
        result = self._llm.generate_structured(
            system=SYSTEM_PROMPT, prompt=prompt, schema=PlannerOutput
        )
        plan = verify_plan(result.output, state["repo_files"])
        if plan.rejected_files:
            logger.info("planner: dropped %d non-existent paths", len(plan.rejected_files))
        step = step_from_llm_result(
            AGENT_NAME,
            result,
            # Record sizes, not the whole file list: steps are stored per run.
            input={"ticket_id": state["ticket_id"], "files_shown": shown, "files_omitted": omitted},
            output=plan.model_dump(mode="json"),
        )
        return {"plan": plan, "steps": [step]}
