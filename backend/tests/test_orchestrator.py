from app.agents.context import RunContext
from app.agents.orchestrator import build_graph, run_orchestrator
from app.agents.schemas import CandidateFileOut, CoderOutput, PlannerOutput, SubtaskOut
from app.agents.state import RunInput
from tests.fakes import LOGIN_PY, FakeFiles, FakeLLM, FakeSearch, default_coder_output

RUN_INPUT: RunInput = {
    "ticket_id": "t-1",
    "repo_connection_id": "r-1",
    "title": "Fix login bug",
    "description": "Steps to reproduce",
    "repo_files": ["src/login.py"],
    "base_commit_sha": "a" * 40,
}


def fake_llm() -> FakeLLM:
    return FakeLLM(
        outputs={
            PlannerOutput: PlannerOutput(
                subtasks=[SubtaskOut(title="Fix", details="Fix the parser")],
                candidate_files=[CandidateFileOut(path="src/login.py", reason="login logic")],
                open_questions=[],
            ),
            CoderOutput: default_coder_output(),
        }
    )


def context() -> RunContext:
    return RunContext(files=FakeFiles({"src/login.py": LOGIN_PY}), search=FakeSearch())


def test_graph_is_start_planner_coder_end():
    nodes = set(build_graph(fake_llm()).get_graph().nodes)
    assert nodes == {"__start__", "planner", "coder", "__end__"}


def test_run_produces_a_plan_a_patch_and_a_step_for_each_agent():
    result = run_orchestrator(RUN_INPUT, context(), graph=build_graph(fake_llm()))
    assert result["title"] == "Fix login bug"
    assert result["plan"].candidate_files[0].path == "src/login.py"
    assert result["patch"].files_changed == 1
    assert result["patch"].base_commit_sha == "a" * 40
    assert [s["agent_name"] for s in result["steps"]] == ["planner", "coder"]


def test_the_coder_runs_after_the_planner_and_sees_its_plan():
    llm = fake_llm()
    run_orchestrator(RUN_INPUT, context(), graph=build_graph(llm))
    assert [c["schema"] for c in llm.calls] == [PlannerOutput, CoderOutput]
    assert "Fix the parser" in llm.calls[1]["prompt"]  # the plan reached the Coder


def test_callers_cannot_inject_internal_state():
    graph = build_graph(fake_llm())
    tampered = {**RUN_INPUT, "steps": [{"agent_name": "injected"}], "plan": "forged"}
    result = run_orchestrator(tampered, context(), graph=graph)  # type: ignore[arg-type]
    assert [s["agent_name"] for s in result["steps"]] == ["planner", "coder"]
    assert result["plan"] != "forged"


def test_runs_do_not_share_state():
    graph = build_graph(fake_llm())
    run_orchestrator(RUN_INPUT, context(), graph=graph)
    second = run_orchestrator(RUN_INPUT, context(), graph=graph)
    assert len(second["steps"]) == 2


def test_each_run_uses_its_own_context():
    """The compiled graph is shared between runs; the files the Coder sees must come only
    from the run's own context."""
    graph_llm = fake_llm()
    graph = build_graph(graph_llm)
    other = RunContext(
        files=FakeFiles({"src/login.py": "# file from another repository\n" + LOGIN_PY}),
        search=FakeSearch(),
    )

    run_orchestrator(RUN_INPUT, context(), graph=graph)
    run_orchestrator(RUN_INPUT, other, graph=graph)

    first_prompt, second_prompt = (c["prompt"] for c in graph_llm.calls_for(CoderOutput))
    assert "another repository" not in first_prompt
    assert "another repository" in second_prompt
