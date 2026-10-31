"""A record of one agent step, shaped like a row of the `agent_steps` table.

Agents append these to the run state; the run service persists them. Keeping agents
free of database code means they can be tested without a database.
"""

from typing import Any, TypedDict

from app.llm.client import LLMResult


class StepRecord(TypedDict):
    agent_name: str
    input: dict[str, Any]
    output: dict[str, Any]
    tokens_used: int | None
    latency_ms: int
    model: str


def step_from_llm_result(
    agent_name: str, result: LLMResult[Any], *, input: dict[str, Any], output: dict[str, Any]
) -> StepRecord:
    return StepRecord(
        agent_name=agent_name,
        input=input,
        output=output,
        tokens_used=result.usage.total_tokens,
        latency_ms=result.latency_ms,
        model=result.model,
    )
