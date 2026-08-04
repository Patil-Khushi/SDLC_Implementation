"""Debugging-path tests: the LLM-fix half of the post-commit debug/test loop.

Mirrors app/tests/test_repair_paths.py's minimal-fake-LLM-gateway pattern: a scriptable LLM
stand-in plus FakeExecutor, no real network/sandbox. Covers: a proposed fix lands under
``<project_dir>/`` for both failure signals (a compile/build failure via ``debug_result`` and a
test failure via ``test_result``), the prompt names the right failing check ("compile/build" vs.
"test") - including when a stale failed ``test_result`` from an earlier loop iteration coexists
with a fresh ``debug_result`` failure, where the fresh signal must win - ``debug_attempt``
increments by exactly one per ``execute()`` call, an unparseable reply triggers exactly ONE
tool-free retry before it counts as "wrote nothing" (D1), a spurious "unknown" failure signal is a
cheap no-op with no LLM call at all (D2), and the debugging report labels a pytest-convention test
path as TEST, not source (D3).
"""

from __future__ import annotations

from typing import Any

from app.agents.debugging import DebuggingAgent
from app.integrations.executor import FakeExecutor


class _FixedReplyLLM:
    """Minimal gateway stand-in: returns one canned fix proposal (or a scripted raw reply) and
    records every call it received, mirroring FakeLLMGateway's ``calls`` recording convention
    (see app/services/llm_gateway.py) so a test can inspect the prompt it was given.

    ``raw`` scripts the (first) ``complete_with_tools`` reply; when omitted it defaults to a valid
    JSON fix for ``path``. ``retry_raw`` separately scripts the tool-free ``complete()`` retry the
    agent now makes on a parse failure (see debugging.py's D1 fix); when omitted IT ALSO defaults
    to the valid JSON fix, so a test that doesn't care about the retry leg never has to think about
    it — only a test deliberately checking "still unparseable after the retry" needs to set it.
    """

    def __init__(self, path: str = "", *, raw: str | None = None, retry_raw: str | None = None) -> None:
        self._path = path
        self._raw = raw
        self._retry_raw = retry_raw
        self.calls: list[dict[str, Any]] = []

    def _default_json(self) -> str:
        return f'{{"files":[{{"path":"{self._path}","content":"print(1)"}}],"notes":"x"}}'

    def complete_with_tools(
        self, prompt: str, *, system: str | None = None, tools: list | None = None, max_iters: int = 4
    ) -> str:
        self.calls.append({"prompt": prompt, "system": system, "tools": tools, "method": "complete_with_tools"})
        return self._raw if self._raw is not None else self._default_json()

    def complete(self, prompt: str, *, system: str | None = None, **_: Any) -> str:
        # Only ever hit by the D1 tool-free retry after a parse failure — see debugging.py's
        # execute(). Kept separate from complete_with_tools's recording so a test can assert
        # exactly which of the two methods was called and how many times.
        self.calls.append({"prompt": prompt, "system": system, "method": "complete"})
        return self._retry_raw if self._retry_raw is not None else self._default_json()


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


def _failing(stderr: str = "boom", stdout: str = "", name: str = "build") -> dict:
    return {"passed": False, "checks": [
        {"name": name, "passed": False, "stderr": stderr, "stdout": stdout, "exit_code": 1}
    ]}


def test_debug_attempt_counts_consecutive_stalled_rounds_not_raw_calls() -> None:
    """The cap counter advances only when a round FAILS to reduce the failure count.

    A flat per-call counter conflated "stuck" with "lots of independent failures" — see the
    module docstring in app/agents/debugging.py.
    """
    executor = FakeExecutor()
    llm = _FixedReplyLLM("backend/app/main.py")
    agent = DebuggingAgent(executor=executor, llm=llm)
    # Same failure count every round => no progress is ever made.
    state = _state(debug_result=_failing(stdout="Tests: 5 failed, 1 passed, 6 total"))

    agent.execute(state)
    # First measured round has no baseline to compare against, so it is not counted as a stall.
    assert state["debug_attempt"] == 0
    assert state["debug_last_failure_count"] == 5

    agent.execute(state)
    assert state["debug_attempt"] == 1  # still 5 failing => stalled
    agent.execute(state)
    assert state["debug_attempt"] == 2

    # Total rounds are tracked separately, and DO count every call (the oscillation backstop).
    assert state["debug_rounds"] == 3


def test_progress_resets_the_stall_counter() -> None:
    executor = FakeExecutor()
    llm = _FixedReplyLLM("backend/app/main.py")
    agent = DebuggingAgent(executor=executor, llm=llm)
    state = _state(debug_result=_failing(stdout="Tests: 9 failed, 0 passed, 9 total"))

    agent.execute(state)                       # baseline 9
    state["debug_result"] = _failing(stdout="Tests: 9 failed, 0 passed, 9 total")
    agent.execute(state)
    assert state["debug_attempt"] == 1         # stalled at 9

    # A round that actually fixed something clears the stall counter, so a converging loop is
    # never cut off by the cap no matter how many independent failures the project has.
    state["debug_result"] = _failing(stdout="Tests: 4 failed, 5 passed, 9 total")
    agent.execute(state)
    assert state["debug_attempt"] == 0
    assert state["debug_last_failure_count"] == 4


def test_a_round_that_makes_things_worse_counts_as_a_stall() -> None:
    executor = FakeExecutor()
    llm = _FixedReplyLLM("backend/app/main.py")
    agent = DebuggingAgent(executor=executor, llm=llm)
    state = _state(debug_result=_failing(stdout="Tests: 3 failed, 7 passed, 10 total"))

    agent.execute(state)
    state["debug_result"] = _failing(stdout="Tests: 8 failed, 2 passed, 10 total")
    agent.execute(state)

    assert state["debug_attempt"] == 1


def test_unparseable_reply_retries_once_tool_free_then_recovers() -> None:
    """D1: an unparseable ``complete_with_tools`` reply used to be a guaranteed wasted round —
    hitting DEBUG_MAX_ITERS without a parseable final reply makes ``llm_gateway.complete_with_tools``
    return whatever prose accompanied the LAST tool-use turn, never JSON (see llm_gateway.py). Now
    the agent makes ONE additional tool-free retry (``self.llm.complete``, no tools) explicitly
    re-asking for the JSON, matching unit_test.py/code_generator.py's own parse-failure retry. Here
    the retry succeeds, so its fix is the one written.
    """
    executor = FakeExecutor()
    llm = _FixedReplyLLM("backend/app/fixed.py", raw="not json at all, just prose")
    state = _state(
        debug_result={"passed": False, "checks": [{"name": "compile", "passed": False, "stderr": "boom", "exit_code": 1}]}
    )

    DebuggingAgent(executor=executor, llm=llm).execute(state)

    assert len(llm.calls) == 2
    assert llm.calls[0]["method"] == "complete_with_tools"       # the original tool-loop call
    assert llm.calls[1]["method"] == "complete"                  # the D1 tool-free retry
    assert "not valid JSON" in llm.calls[1]["prompt"]             # retry names the parse failure
    assert "proj/backend/app/fixed.py" in executor.files          # retry's fix WAS written


def test_unparseable_reply_after_retry_still_writes_nothing_and_does_not_raise() -> None:
    """D1's flip side: when even the tool-free retry fails to parse, the round still counts as
    "wrote nothing" (not a crash, not partial garbage) — same externally-visible outcome as
    before D1, just reached after one extra cheap call instead of silently accepting whatever
    prose the exhausted tool loop happened to return."""
    executor = FakeExecutor()
    llm = _FixedReplyLLM(raw="not json at all, just prose", retry_raw="still not json either")
    state = _state(
        debug_result={"passed": False, "checks": [{"name": "compile", "passed": False, "stderr": "boom", "exit_code": 1}]}
    )

    DebuggingAgent(executor=executor, llm=llm).execute(state)  # must not raise

    assert len(llm.calls) == 2
    assert executor.writes == []
    assert executor.files == {}


def test_unknown_check_is_a_cheap_no_op_no_llm_call_no_round_counted() -> None:
    """D2: ``_current_failure`` returns ("unknown", "", "") when neither ``debug_result`` nor
    ``test_result`` is actually failing — shouldn't normally happen given the router, but nothing
    enforces it, and being wrong here used to cost a full DEBUG_MAX_ITERS-turn LLM round for zero
    signal. It must now short-circuit before any LLM call and before any counter moves."""
    executor = FakeExecutor()
    llm = _FixedReplyLLM("backend/app/main.py")
    state = _state()  # no debug_result, no test_result -> _current_failure returns "unknown"

    result = DebuggingAgent(executor=executor, llm=llm).execute(state)

    assert llm.calls == []                        # no LLM call at all, tool or tool-free
    assert result.get("debug_rounds", 0) == 0
    assert result.get("debug_attempt", 0) == 0
    assert executor.writes == []
    assert "debugging_report" not in result        # no report entry for a round that never ran


def test_debugging_report_flags_pytest_style_test_path_as_test_not_source() -> None:
    """D3: debugging.py used to keep its OWN, narrower ``_is_test_path`` (only ``.test.``/``.spec.``/
    ``/__tests__/`` — the JS conventions), so a pytest-style edit like ``test_login.py`` was
    silently mislabeled "source" in the debugging report/summary even though the report's whole
    purpose is to flag test edits distinctly. It now reuses plan_builder's broader definition
    (also catches ``test_*.py``, ``*_test.py``, and a bare ``tests/``/``__tests__/`` path segment).
    """
    executor = FakeExecutor()
    # No "tests/" directory segment and no ".test."/".spec." substring — only the pytest
    # test_*.py filename convention marks this as a test file, which the OLD debugging.py
    # _is_test_path could not detect at all.
    llm = _FixedReplyLLM("app/services/test_login.py")
    state = _state(
        debug_result={"passed": False, "checks": [{"name": "test", "passed": False, "stderr": "boom", "exit_code": 1}]}
    )

    DebuggingAgent(executor=executor, llm=llm).execute(state)

    assert "1 of them TEST file(s)" in state["generation_summary"]
    assert "| TEST |" in state["debugging_report"]
