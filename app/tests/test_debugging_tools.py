"""Debugging agent: fixes land through tools, the whole failure reaches the model, and nothing
crashes the graph.

These cover the failure modes seen in a real run: rounds 2-9 wrote nothing because the one-JSON-
reply-with-every-file format was cut off and discarded; a pip build failure behind a tsc failure
never reached the model; progress read as "2 failing" whether 209 or 109 errors remained; and a
provider error crashed the whole graph instead of ending one round.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from app.agents.debugging import DEBUG_MAX_ITERS, DEBUG_MAX_ITERS_CEILING, DebuggingAgent, _failure_count
from app.integrations.executor import FakeExecutor

TSC = (
    "[tsc[proj/frontend]] src/pages/Login.tsx(12,5): error TS2322: bad variant\n"
    "src/pages/Login.tsx(30,1): error TS2304: Cannot find name 'foo'.\n"
    "src/App.tsx(3,20): error TS2307: Cannot find module './store'.\n"
)
PIP = "[pip[proj/backend]] error: Microsoft Visual C++ 14.0 or greater is required.\nERROR: Failed building wheel for asyncpg\n"

Action = tuple[str, dict[str, Any]]


class _ToolUsingLLM:
    """Gateway double that CALLS the tools it is given, like the real tool loop does.

    ``actions`` are ``(tool_name, kwargs)`` pairs run in order; then it returns ``final`` — or
    raises ``raise_after`` to simulate the provider failing mid-session.
    """

    def __init__(self, actions: list[Action] | None = None, *, final: str = "Fixed Login.tsx.",
                 raise_after: Exception | None = None, stop_reason: str = "end_turn") -> None:
        self.actions = actions or []
        self.final = final
        self.raise_after = raise_after
        self.last_stop_reason: str | None = None
        self._stop = stop_reason
        self.tool_names: list[str] = []
        self.max_iters: int | None = None
        self.prompts: list[str] = []
        self.results: list[str] = []
        self.complete_calls = 0

    def complete_with_tools(self, prompt: str, *, system: str | None = None, tools: list | None = None,
                            max_iters: int = 6) -> str:
        self.prompts.append(prompt)
        self.max_iters = max_iters
        by_name: dict[str, Callable[..., Any]] = {t.name: t.handler for t in tools or []}
        self.tool_names = [t.name for t in tools or []]
        for name, kwargs in self.actions:
            self.results.append(str(by_name[name](**kwargs)))
        self.last_stop_reason = self._stop
        if self.raise_after is not None:
            raise self.raise_after
        return self.final

    def complete(self, prompt: str, *, system: str | None = None, **_: Any) -> str:
        self.complete_calls += 1
        return "still nothing"


def _state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"run_id": "r1", "project_id": "proj", "generated_code": [], "debug_attempt": 0}
    base.update(over)
    return base


def _compile_build_failure() -> dict[str, Any]:
    return {
        "passed": False,
        "checks": [
            {"name": "compile", "passed": False, "stderr": TSC, "stdout": "", "exit_code": 2},
            {"name": "build", "passed": False, "stderr": PIP, "stdout": "", "exit_code": 1},
        ],
    }


@pytest.fixture(autouse=True)
def _reports_in_tmp(tmp_path, monkeypatch) -> None:
    from app.config import settings as settings_mod

    monkeypatch.setattr(settings_mod.get_settings(), "reports_dir", str(tmp_path / "reports"), raising=False)


def test_fixes_written_through_tools_land_immediately_and_are_recorded() -> None:
    executor = FakeExecutor(files={"proj/frontend/src/pages/Login.tsx": "const v = 'ghost';\nfoo();\n"})
    llm = _ToolUsingLLM([
        ("read_file", {"path": "frontend/src/pages/Login.tsx"}),
        ("edit_file", {"path": "frontend/src/pages/Login.tsx", "old": "'ghost'", "new": "'secondary'"}),
        ("write_file", {"path": "frontend/src/store.ts", "content": "export const store = {};\n"}),
    ])
    state = _state(debug_result=_compile_build_failure(), generated_code=["proj/frontend/src/pages/Login.tsx"])

    DebuggingAgent(executor=executor, llm=llm).execute(state)

    assert executor.files["proj/frontend/src/pages/Login.tsx"] == "const v = 'secondary';\nfoo();\n"
    assert executor.files["proj/frontend/src/store.ts"] == "export const store = {};\n"
    assert "proj/frontend/src/store.ts" in state["generated_code"]
    assert llm.complete_calls == 0                      # no JSON retry when the tools did the work
    assert "Files changed (2)" in state["debugging_report"]
    assert "Fixed Login.tsx." in state["debugging_report"]   # the plain-text summary is the notes


def test_tool_list_has_one_read_file_and_offers_edit_file() -> None:
    llm = _ToolUsingLLM()
    DebuggingAgent(executor=FakeExecutor(), llm=llm).execute(_state(debug_result=_compile_build_failure()))
    assert llm.tool_names.count("read_file") == 1
    assert {"write_file", "edit_file", "git_status", "git_diff", "run_command"} <= set(llm.tool_names)


def test_prompt_carries_every_failing_check_errors_by_file_and_environment() -> None:
    llm = _ToolUsingLLM()
    DebuggingAgent(executor=FakeExecutor(), llm=llm).execute(_state(debug_result=_compile_build_failure()))
    prompt = llm.prompts[0]
    assert "Output of the failing `compile` check" in prompt
    assert "Output of the failing `build` check" in prompt          # the second failure is no longer hidden
    assert "| 2 | frontend/src/pages/Login.tsx |" in prompt           # worst file first, project-relative
    assert "ENVIRONMENTAL" in prompt and "C compiler" in prompt
    assert '"files"' not in prompt                                     # no JSON reply format demanded


def test_stdout_already_inside_stderr_is_not_repeated() -> None:
    llm = _ToolUsingLLM()
    check = {"name": "test", "passed": False, "stderr": "[pytest[proj/backend]] UNIQUE-MARKER 1 failed in 0.1s",
             "stdout": "UNIQUE-MARKER 1 failed in 0.1s"}
    DebuggingAgent(executor=FakeExecutor(), llm=llm).execute(
        _state(test_result={"passed": False, "checks": [check]})
    )
    assert llm.prompts[0].count("UNIQUE-MARKER") == 1


def test_llm_exception_ends_the_round_without_raising_and_keeps_earlier_writes() -> None:
    executor = FakeExecutor()
    llm = _ToolUsingLLM(
        [("write_file", {"path": "frontend/src/store.ts", "content": "export {};\n"})],
        raise_after=ConnectionError("Connection error."),
    )
    state = _state(debug_result=_compile_build_failure())

    DebuggingAgent(executor=executor, llm=llm).execute(state)          # must not raise

    assert executor.files["proj/frontend/src/store.ts"] == "export {};\n"   # the write before the error survives
    assert "proj/frontend/src/store.ts" in state["generated_code"]
    assert "LLM call failed" in state["generation_summary"]
    assert llm.complete_calls == 0                                       # no retry after a provider failure


def test_llm_exception_with_no_writes_is_recorded_not_raised() -> None:
    llm = _ToolUsingLLM(raise_after=TimeoutError("read timed out"))
    state = _state(debug_result=_compile_build_failure())
    DebuggingAgent(executor=FakeExecutor(), llm=llm).execute(state)
    assert "the LLM call failed: `TimeoutError: read timed out`" in state["debugging_report"]
    assert state["debug_rounds"] == 1


def test_empty_round_report_says_why_including_stop_reason() -> None:
    llm = _ToolUsingLLM(final="I looked around but changed nothing.", stop_reason="max_tokens")
    state = _state(debug_result=_compile_build_failure())
    DebuggingAgent(executor=FakeExecutor(), llm=llm).execute(state)
    assert llm.complete_calls == 1                                       # the one tool-free fallback ran
    assert "stop reason: `max_tokens`" in state["debugging_report"]
    assert "| Stop reason | max_tokens |" in state["debugging_report"]


def test_failure_count_is_quantified_so_halving_the_errors_is_progress() -> None:
    state = _state(debug_result=_compile_build_failure())
    assert _failure_count(state) == 3 + 1                 # three tsc error lines + the pip failure
    agent = DebuggingAgent(executor=FakeExecutor(), llm=_ToolUsingLLM())
    agent.execute(state)                                   # round 1: baseline 4
    fewer = _compile_build_failure()
    fewer["checks"][0]["stderr"] = "[tsc[proj/frontend]] src/App.tsx(3,20): error TS2307: x\n"
    state["debug_result"] = fewer
    agent.execute(state)                                   # round 2: 2 < 4 -> progress, counter stays 0
    assert state["debug_last_failure_count"] == 2
    assert state["debug_attempt"] == 0


def test_tool_budget_scales_with_files_that_have_errors() -> None:
    assert DebuggingAgent._max_iters_for(0) == DEBUG_MAX_ITERS
    assert DebuggingAgent._max_iters_for(3) == DEBUG_MAX_ITERS + 6
    assert DebuggingAgent._max_iters_for(200) == DEBUG_MAX_ITERS_CEILING
    llm = _ToolUsingLLM()
    DebuggingAgent(executor=FakeExecutor(), llm=llm).execute(_state(debug_result=_compile_build_failure()))
    assert llm.max_iters == DebuggingAgent._max_iters_for(2)            # Login.tsx + App.tsx
