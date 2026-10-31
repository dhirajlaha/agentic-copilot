from app.agents.orchestrator import build_graph, run_orchestrator
from app.agents.schemas import CandidateFileOut, PlannerOutput, SubtaskOut
from app.agents.state import RunInput
from tests.fakes import FakeLLM

RUN_INPUT: RunInput = {
    "ticket_id": "t-1",
    "repo_connection_id": "r-1",
    "title": "Fix login bug",
    "description": "Steps to reproduce",
    "repo_files": ["src/login.py"],
}


def fake_llm() -> FakeLLM:
    return FakeLLM(
        PlannerOutput(
            subtasks=[SubtaskOut(title="Fix", details="Fix the parser")],
            candidate_files=[CandidateFileOut(path="src/login.py", reason="login logic")],
            open_questions=[],
        )
    )


def test_graph_is_start_planner_end():
    assert set(build_graph(fake_llm()).get_graph().nodes) == {"__start__", "planner", "__end__"}


def test_run_produces_a_plan_and_one_step():
    result = run_orchestrator(RUN_INPUT, graph=build_graph(fake_llm()))
    assert result["title"] == "Fix login bug"
    assert result["plan"].candidate_files[0].path == "src/login.py"
    assert [s["agent_name"] for s in result["steps"]] == ["planner"]


def test_callers_cannot_inject_internal_state():
    graph = build_graph(fake_llm())
    tampered = {**RUN_INPUT, "steps": [{"agent_name": "injected"}], "plan": "forged"}
    result = run_orchestrator(tampered, graph=graph)  # type: ignore[arg-type]
    assert [s["agent_name"] for s in result["steps"]] == ["planner"]
    assert result["plan"] != "forged"


def test_runs_do_not_share_state():
    graph = build_graph(fake_llm())
    run_orchestrator(RUN_INPUT, graph=graph)
    assert len(run_orchestrator(RUN_INPUT, graph=graph)["steps"]) == 1
