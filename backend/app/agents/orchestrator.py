"""The agent graph: a LangGraph state machine with one node per agent.

Today: START -> planner -> coder -> END. Later: Test Generator, Test Executor and Reporter,
plus a conditional edge that sends test failures back to the Coder (max 2 retries, then
`needs_human_review`). Retrieval (RAG) is not a node: the Coder searches the code index
itself, through the per-run context.
"""

from functools import lru_cache

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.coder import AGENT_NAME as CODER
from app.agents.coder import CoderAgent
from app.agents.context import CONTEXT_KEY, RunContext
from app.agents.planner import AGENT_NAME as PLANNER
from app.agents.planner import PlannerAgent
from app.agents.state import RunInput, RunState
from app.core.config import get_llm_settings
from app.llm.client import LLMClient
from app.llm.factory import build_llm_client


def build_graph(llm: LLMClient) -> CompiledStateGraph:
    """Assemble the graph. The LLM client is injected, so tests pass a fake."""
    builder = StateGraph(RunState, input_schema=RunInput)
    builder.add_node(PLANNER, PlannerAgent(llm))
    builder.add_node(CODER, CoderAgent(llm))
    builder.add_edge(START, PLANNER)
    builder.add_edge(PLANNER, CODER)
    builder.add_edge(CODER, END)
    return builder.compile()


@lru_cache
def get_orchestrator() -> CompiledStateGraph:
    """The production graph, wired to the configured LLM. Compiled once per process: the
    compiled graph holds no per-run data, so sharing it is safe."""
    return build_graph(build_llm_client(get_llm_settings()))


def run_orchestrator(
    run_input: RunInput,
    context: RunContext,
    graph: CompiledStateGraph | None = None,
) -> RunState:
    """Execute the graph for one ticket and return the final state."""
    config = {"configurable": {CONTEXT_KEY: context}}
    return (graph or get_orchestrator()).invoke(run_input, config)  # type: ignore[return-value]
