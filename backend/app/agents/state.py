"""Shared state that flows through the agent graph.

Every node receives the current state and returns a *partial* update; LangGraph merges
the updates. Fields with a reducer (`Annotated[..., reducer]`) are combined instead of
overwritten. Fields for later agents are added when those agents are built.
"""

import operator
from typing import Annotated, NotRequired, TypedDict

from app.agents.schemas import Patch, Plan
from app.agents.steps import StepRecord


class RunInput(TypedDict):
    """What a caller must supply to start a run. Anything else is ignored, so callers
    cannot inject internal state (a plan, step records)."""

    ticket_id: str
    repo_connection_id: str
    title: str
    description: str
    # Paths in the connected repository. Supplied by the repo-connect flow (GitHub App
    # installation, week 2); the Planner only sees names, not contents.
    repo_files: list[str]
    # The commit the code index and every file the agents read are pinned to, so a patch is
    # checked against exactly the code that was planned against.
    base_commit_sha: str


class RunState(RunInput):
    plan: NotRequired[Plan]  # set by the Planner
    patch: NotRequired[Patch]  # set by the Coder
    # One record per agent step, appended in order (persisted to agent_steps).
    steps: Annotated[list[StepRecord], operator.add]
