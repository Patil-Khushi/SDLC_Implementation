"""Debugging-path tests: the LLM-fix half of the post-commit debug/test loop.

Mirrors app/tests/test_repair_paths.py's minimal-fake-LLM-gateway pattern: a scriptable LLM
stand-in plus FakeExecutor, no real network/sandbox. Covers: a proposed fix lands under
``<project_dir>/`` for both failure signals (a compile/build failure via ``debug_result`` and a
test failure via ``test_result``), the prompt names the right failing check ("compile/build" vs.
"test") - including when a stale failed ``test_result`` from an earlier loop iteration coexists
with a fresh ``debug_result`` failure, where the fresh signal must win - ``debug_attempt``
increments by exactly one per ``execute()`` call, and an unparseable reply writes nothing without
raising.
"""

from __future__ import annotations

from typing import Any

from app.agents.debugging import DebuggingAgent
from app.integrations.executor import FakeExecutor


class _FixedReplyLLM:
    """Minimal gateway stand-in: returns one canned fix proposal (or a scripted raw reply) and
    records every call it received, mirroring FakeLLMGateway's ``calls`` recording convention
    (see app/services/llm_gateway.py) so a test can inspect the prompt it was given."""

    def __init__(self, path: str = "", *, raw: str | None = None) -> None:
        self._path = path
        self._raw = raw
        self.calls: list[dict[str, Any]] = []

    def complete_with_tools(
        self, prompt: str, *, system: str | None = None, tools: list | None = None, max_iters: int = 4
    ) -> str:
        self.calls.append({"prompt": prompt, "system": system, "tools": tools})
        if self._raw is not None:
            return self._raw
        return f'{{"files":[{{"path":"{self._path}","content":"print(1)"}}],"notes":"x"}}'


def _state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": "r1",
        "project_id": "proj",
        "generated_code": [],
        "debug_attempt": 0,
    }
    base.update(over)
    return base


def test_debugging_writes_fix_under_project_dir_on_compile_build_failure() -> None:
    executor = FakeExecutor()
    state = _state(
        debug_result={
            "passed": False,
            "checks": [{"name": "compile", "passed": False, "stderr": "SyntaxError: bad token", "exit_code": 1}],
        }
    )
    llm = _FixedReplyLLM("backend/app/main.py")

    DebuggingAgent(executor=executor, llm=llm).execute(state)

    assert "proj/backend/app/main.py" in executor.files       # written where the gate looks
    assert "backend/app/main.py" not in executor.files         # NOT at the bare path
    assert executor.files_complete("proj", ["backend/app/main.py"]).passed
    assert "proj/backend/app/main.py" in state["generated_code"]


def test_debugging_writes_fix_on_test_failure_and_prompt_names_test() -> None:
    executor = FakeExecutor()
    # compile/build passed; the test suite is what failed — the more specific signal must win.
    state = _state(
        debug_result={"passed": True, "checks": [{"name": "compile", "passed": True, "stderr": "", "exit_code": 0}]},
        test_result={
            "passed": False,
            "checks": [{"name": "test", "passed": False, "stderr": "AssertionError: expected 2 got 1", "exit_code": 1}],
        },
    )
    llm = _FixedReplyLLM("backend/app/util.py")

    DebuggingAgent(executor=executor, llm=llm).execute(state)

    assert "proj/backend/app/util.py" in executor.files
    assert len(llm.calls) == 1
    prompt = llm.calls[0]["prompt"]
    assert "test" in prompt
    assert "compile/build" not in prompt


def test_fresh_debug_result_failure_outranks_stale_test_result_failure() -> None:
    """Regression: test_result is only overwritten by unit_test_run_node, so a failed test_result
    from an earlier loop iteration can still be sitting in state after a later fix introduces a
    fresh debug_result (compile/build) failure. The fresh, live signal must win, not the stale one."""
    executor = FakeExecutor()
    state = _state(
        debug_result={
            "passed": False,
            "checks": [{"name": "build", "passed": False, "stderr": "ImportError: no module named foo", "exit_code": 1}],
        },
        test_result={  # stale: left over from an earlier iteration, no longer the live problem
            "passed": False,
            "checks": [{"name": "test", "passed": False, "stderr": "AssertionError: expected 2 got 1", "exit_code": 1}],
        },
    )
    llm = _FixedReplyLLM("backend/app/foo.py")

    DebuggingAgent(executor=executor, llm=llm).execute(state)

    assert len(llm.calls) == 1
    prompt = llm.calls[0]["prompt"]
    assert "compile/build" in prompt
    assert "ImportError: no module named foo" in prompt
    assert "AssertionError: expected 2 got 1" not in prompt  # stale signal must not leak in


def test_debug_attempt_increments_by_one_per_execute_call() -> None:
    executor = FakeExecutor()
    llm = _FixedReplyLLM("backend/app/main.py")
    agent = DebuggingAgent(executor=executor, llm=llm)
    state = _state(
        debug_result={"passed": False, "checks": [{"name": "build", "passed": False, "stderr": "boom", "exit_code": 1}]}
    )

    agent.execute(state)
    assert state["debug_attempt"] == 1

    agent.execute(state)
    assert state["debug_attempt"] == 2


def test_unparseable_reply_writes_nothing_and_does_not_raise() -> None:
    executor = FakeExecutor()
    llm = _FixedReplyLLM(raw="not json at all, just prose")
    state = _state(
        debug_result={"passed": False, "checks": [{"name": "compile", "passed": False, "stderr": "boom", "exit_code": 1}]}
    )

    DebuggingAgent(executor=executor, llm=llm).execute(state)  # must not raise

    assert executor.writes == []
    assert executor.files == {}
