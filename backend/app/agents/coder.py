"""Coder agent: turns the plan into a checked patch.

Flow: read the full text of the files the Planner picked (never anything the secret policy
protects) -> retrieve related code from the index -> ask the model for exact-text edits ->
check every edit against the real files -> if anything is wrong, show the model its mistakes
and ask again (up to two repairs) -> return a verified `Patch`.

The model only ever proposes. Nothing reaches the repository from here; publishing is a
separate step.
"""

import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agents.context import FileReader, RunContext, run_context_from
from app.agents.patching import Problem, build_patch, path_problem
from app.agents.schemas import CoderOutput, Patch
from app.agents.state import RunState
from app.agents.steps import StepRecord
from app.core.exceptions import AppError, CodingError
from app.indexing import policy
from app.llm.client import LLMClient, LLMResult
from app.repositories.chunk_repository import ChunkHit

logger = logging.getLogger(__name__)

AGENT_NAME = "coder"
SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "coder_system.md").read_text()

MAX_ATTEMPTS = 3  # the first try plus two repairs
MAX_SHOWN_CHARS = 200_000  # full file text sent to the model (about 60k tokens)
MAX_CONTEXT_CHARS = 60_000  # retrieved snippets sent to the model
CHUNKS_PER_QUERY = 8
MAX_PREVIOUS_ANSWER_CHARS = 20_000


class _Shown:
    """The files put in front of the model, and the ones left out (with why)."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.skipped: dict[str, str] = {}


def _read_candidates(state: RunState, files: FileReader) -> _Shown:
    shown = _Shown()
    budget = MAX_SHOWN_CHARS
    for candidate in state["plan"].candidate_files:
        path = candidate.path
        if (problem := path_problem(path)) is not None:
            shown.skipped[path] = problem
            continue
        try:
            text = files.read_text(path)
        except AppError as exc:
            shown.skipped[path] = str(exc)
            continue
        if text is None:
            shown.skipped[path] = "not found at this commit"
        elif policy.contains_secret(text):
            shown.skipped[path] = "looks like it contains a credential"
        elif len(text) > budget:
            shown.skipped[path] = "does not fit in the model's context budget"
        else:
            shown.files[path] = text
            budget -= len(text)
    return shown


def _retrieve(state: RunState, search_fn: Any, shown: _Shown) -> tuple[list[ChunkHit], int]:
    """Related code for the ticket and for each sub-task, best matches first, skipping
    files already shown in full. Returns (chunks, number of queries)."""
    plan = state["plan"]
    queries = [f"{state['title']}\n{state['description']}".strip()]
    queries += [f"{s.title}\n{s.details}" for s in plan.subtasks]
    best: dict[tuple[str, int], ChunkHit] = {}
    for query in queries:
        for hit in search_fn.search(query[:4000], CHUNKS_PER_QUERY):
            if hit.file_path in shown.files:
                continue
            key = (hit.file_path, hit.start_line)
            if key not in best or hit.distance < best[key].distance:
                best[key] = hit
    chosen: list[ChunkHit] = []
    used = 0
    for hit in sorted(best.values(), key=lambda h: h.distance):
        if used + len(hit.content) > MAX_CONTEXT_CHARS:
            break
        chosen.append(hit)
        used += len(hit.content)
    return chosen, len(queries)


def build_user_prompt(
    state: RunState, shown: _Shown, chunks: list[ChunkHit], nonce: str, repair: str = ""
) -> str:
    # JSON-escape "<" so ticket text cannot close our block, as in the Planner.
    ticket = json.dumps(
        {"title": state["title"], "description": state["description"]}, ensure_ascii=False
    ).replace("<", "\\u003c")
    plan = state["plan"]
    steps = "\n".join(f"{i}. {s.title}: {s.details}" for i, s in enumerate(plan.subtasks, 1))
    questions = "\n".join(f"- {q}" for q in plan.open_questions)
    parts = [f"<ticket>\n{ticket}\n</ticket>", f"<plan>\n{steps}\n</plan>"]
    if questions:
        parts.append(f"<planner_open_questions>\n{questions}\n</planner_open_questions>")

    # Code can contain any text, including our closing tags, so blocks are fenced with a
    # random id that the code cannot know.
    parts.append(f"Files you may change, with their full current contents (block id {nonce}):")
    for path, text in shown.files.items():
        parts.append(f"=====[{nonce}] FILE {path} =====\n{text}\n=====[{nonce}] END FILE =====")
    if not shown.files:
        parts.append("(no file contents are available)")
    if shown.skipped:
        omitted = "\n".join(f"- {p}: {why}" for p, why in shown.skipped.items())
        parts.append(f"Files the plan mentioned that you cannot see:\n{omitted}")
    if chunks:
        parts.append(
            f"Related code found by searching the repository (read-only context, block id {nonce}):"
        )
        for hit in chunks:
            label = f"{hit.file_path}:{hit.start_line}-{hit.end_line}"
            parts.append(
                f"=====[{nonce}] CONTEXT {label} =====\n{hit.content}\n"
                f"=====[{nonce}] END CONTEXT ====="
            )
    parts.append(repair or "Write the change.")
    return "\n\n".join(parts)


def _repair_section(previous: CoderOutput, problems: list[Problem]) -> str:
    listed = "\n".join(f"- {p}" for p in problems)
    answer = previous.model_dump_json(indent=1)[:MAX_PREVIOUS_ANSWER_CHARS]
    return (
        "Your previous answer was rejected because it could not be applied:\n"
        f"{listed}\n\n"
        "Your previous answer, for reference:\n"
        f"{answer}\n\n"
        "Fix every problem and return the complete corrected change (not just the fixes)."
    )


class CoderAgent:
    """A LangGraph node. Stateless apart from its injected LLM client; the per-run files
    and search arrive through the run context."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def __call__(self, state: RunState, config: RunnableConfig) -> dict[str, Any]:
        context = run_context_from(config)
        shown = _read_candidates(state, context.files)
        chunks, queries = _retrieve(state, context.search, shown)
        nonce = secrets.token_hex(8)

        results: list[LLMResult[CoderOutput]] = []
        repair = ""
        patch: Patch | None = None
        problems: list[Problem] = []
        started = time.perf_counter()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            prompt = build_user_prompt(state, shown, chunks, nonce, repair)
            # Writing correct code is where deliberation pays for itself.
            result = self._llm.generate_structured(
                system=SYSTEM_PROMPT, prompt=prompt, schema=CoderOutput, thinking=True
            )
            results.append(result)
            patch, problems = self._check(state, result.output, context, attempt)
            if patch is not None:
                break
            logger.info("coder attempt %d rejected: %d problem(s)", attempt, len(problems))
            repair = _repair_section(result.output, problems)

        if patch is None:
            shown_problems = "; ".join(str(p) for p in problems[:3])
            raise CodingError(
                f"The model could not produce a valid patch after {MAX_ATTEMPTS} attempts "
                f"({shown_problems})"
            )
        return {
            "patch": patch,
            "steps": [self._step(state, results, patch, shown, chunks, queries, started)],
        }

    @staticmethod
    def _check(
        state: RunState, output: CoderOutput, context: RunContext, attempt: int
    ) -> tuple[Patch | None, list[Problem]]:
        patch, problems = build_patch(
            output,
            base_commit_sha=state["base_commit_sha"],
            read_file=context.files.read_text,
            attempts=attempt,
        )
        if patch is not None and not patch.changes and not patch.open_questions:
            return None, [
                Problem(
                    "(patch)",
                    "no changes and no open questions: either change something or explain in "
                    "open_questions why no change can be made",
                )
            ]
        return patch, problems

    @staticmethod
    def _step(
        state: RunState,
        results: list[LLMResult[CoderOutput]],
        patch: Patch,
        shown: _Shown,
        chunks: list[ChunkHit],
        queries: int,
        started: float,
    ) -> StepRecord:
        tokens = [r.usage.total_tokens for r in results]
        return StepRecord(
            agent_name=AGENT_NAME,
            input={
                "ticket_id": state["ticket_id"],
                "files_shown": list(shown.files),
                "files_skipped": shown.skipped,
                "context_chunks": len(chunks),
                "search_queries": queries,
            },
            output=patch.model_dump(mode="json"),
            tokens_used=None if None in tokens else sum(t for t in tokens if t is not None),
            latency_ms=round((time.perf_counter() - started) * 1000),
            model=results[-1].model,
        )
