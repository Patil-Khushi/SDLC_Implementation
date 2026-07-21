"""End-to-end tests for the post-commit Debugging<->Unit-Test loop.

Drives the FULL compiled graph (``app.graph.graph.workflow``) exactly like ``test_graph.py``:
a ``FakeExecutor`` stands in for the sandbox (scriptable ``compile_results``/``build_results``/
``test_results`` queues, consumed in order) and the ``llm_gateway`` singleton's ``complete`` /
``complete_with_tools`` are monkeypatched — no Docker, no real model.

The tricky bit: ``CodeGeneratorAgent`` (per-work-item generation) and ``UnitTestAgent``
(post-commit test generation) both call the SAME ``self.llm.complete()`` method on the shared
gateway singleton, so a single scripted response can't tell them apart by call order alone. The
``_ScriptedLLM`` stand-in below distinguishes them by a fingerprint unique to each agent's own
prompt-building code (``UnitTestAgent._build_prompt`` always emits the literal line
"Source file(s) to test:" — see app/agents/unit_test.py — which ``CodeGeneratorAgent._build_prompt``
never does), and separately counts ``complete_with_tools`` calls (the Debugging agent's only
entry point — see app/agents/debugging.py) so a test can assert "debugging ran exactly once"
independent of "unit-test generation ran exactly once".

Covers (CLAUDE.md / app/graph/router.py's ``DEBUG_CAP = 3``):
1. Happy path: compile/build + tests all pass first try -> "completed".
2. compile/build fails once then passes -> ONE debugging call, debug_attempt == 1, "completed".
3. compile/build passes but the test run fails once then passes -> debugging fixes the SOURCE
   (not the tests); unit tests are NOT regenerated a second time; ends "completed".
4. Cap exhaustion: compile keeps failing past DEBUG_CAP -> "needs_human_review" (escalate), no
   infinite loop.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.graph.graph import workflow
from app.graph.router import DEBUG_CAP
from app.graph.state import new_state
from app.integrations.executor import FakeExecutor, set_executor
from app.models import WorkItem
from app.services import llm_gateway

ITEM = WorkItem(
    id="WI-100",
    requirement_ids=["REQ-100"],
    endpoints=["GET /thing"],
    target_files=["app/api/thing.py"],
)

CODEGEN_JSON = json.dumps({"files": [{"path": "app/api/thing.py", "content": "# v1\n"}], "notes": ""})
TEST_JSON = json.dumps(
    {"files": [{"path": "tests/test_thing.py", "content": "def test_thing():\n    assert True\n"}], "notes": ""}
)
FIX_JSON = json.dumps({"files": [{"path": "app/api/thing.py", "content": "# fixed\n"}], "notes": "fixed"})


class _ScriptedLLM:
    """Bound onto the ``llm_gateway`` singleton's ``complete``/``complete_with_tools`` slots.

    ``complete()`` serves BOTH code-generation (CodeGeneratorAgent) and test-generation
    (UnitTestAgent) prompts — they are told apart by the "Source file(s) to test:" fingerprint
    that only UnitTestAgent's prompt contains. ``complete_with_tools()`` is only ever called by
    the Debugging agent's repair-tool-bound path.
    """

    def __init__(self) -> None:
        self.codegen_calls = 0
        self.test_gen_calls = 0
        self.debug_calls = 0

    def complete(self, prompt: str, *, system: str | None = None, **_: Any) -> str:
        if "Source file(s) to test:" in prompt:
            self.test_gen_calls += 1
            return TEST_JSON
        self.codegen_calls += 1
        return CODEGEN_JSON

    def complete_with_tools(
        self, prompt: str, *, system: str | None = None, tools: list | None = None, max_iters: int = 4
    ) -> str:
        self.debug_calls += 1
        return FIX_JSON


@pytest.fixture(autouse=True)
def stub_llm(monkeypatch):
    """Stub both gateway entry points on the singleton; always tear down the executor after."""
    llm = _ScriptedLLM()
    monkeypatch.setattr(llm_gateway.llm_gateway, "complete", llm.complete)
    monkeypatch.setattr(llm_gateway.llm_gateway, "complete_with_tools", llm.complete_with_tools)
    yield llm
    set_executor(None)


def _invoke(executor: FakeExecutor, thread_id: str) -> dict:
    """Fresh invoke of the full graph; runs to completion (no HITL pause)."""
    set_executor(executor)
    initial = new_state(run_id="run-1", attempt=3, project_id="p1")
    initial["work_items"] = [ITEM]
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
    workflow.invoke(initial, config)
    return dict(workflow.get_state(config).values)


def test_happy_path_compile_build_and_tests_pass_first_try(stub_llm) -> None:
    # No queues scripted -> FakeExecutor's default_pass=True answers every compile/build/test call.
    executor = FakeExecutor()
    final = _invoke(executor, "t-happy")

    assert final["workflow_status"] == "completed"
    assert final["debug_result"]["passed"] is True
    assert final["test_result"]["passed"] is True
    assert final["unit_tests"]                                   # non-empty
    assert stub_llm.debug_calls == 0                              # never entered the debugging path
    assert final.get("debug_attempt", 0) == 0
    assert len(executor.commits) == 1                             # single run-level commit


def test_compile_build_fails_once_then_passes(stub_llm) -> None:
    # compile fails on the first debug_check, passes on the second (post-debugging) recheck.
    executor = FakeExecutor(compile_results=[False, True])
    final = _invoke(executor, "t-compile-once")

    assert stub_llm.debug_calls == 1                              # debugging invoked exactly once
    assert final["debug_attempt"] == 1
    assert final["workflow_status"] == "completed"
    assert final["test_result"]["passed"] is True
    assert final["unit_tests"]
    assert stub_llm.test_gen_calls == 1                           # tests generated normally, once


def test_test_run_fails_once_then_passes_debugging_fixes_source_not_tests(stub_llm) -> None:
    # compile/build always pass; the test run fails once, then passes on the recheck.
    executor = FakeExecutor(test_results=[False, True])
    final = _invoke(executor, "t-test-once")

    assert stub_llm.debug_calls == 1                              # debugging fixed the SOURCE once
    assert final["debug_attempt"] == 1
    # Unit tests were generated exactly once — NOT regenerated after the debugging round-trip.
    assert stub_llm.test_gen_calls == 1
    unit_tests_after = final["unit_tests"]
    assert len(unit_tests_after) == 1
    assert unit_tests_after[0].endswith("tests/test_thing.py")
    assert final["workflow_status"] == "completed"
    assert final["test_result"]["passed"] is True


def test_cap_exhaustion_escalates_to_needs_human_review(stub_llm) -> None:
    # compile keeps failing across every debug_check call (initial + one per debugging attempt),
    # so debug_attempt climbs to DEBUG_CAP without ever passing -> escalate, never loops forever.
    executor = FakeExecutor(compile_results=[False] * (DEBUG_CAP + 3))
    final = _invoke(executor, "t-cap")

    assert final["workflow_status"] == "needs_human_review"
    assert final["debug_attempt"] == DEBUG_CAP
    assert stub_llm.debug_calls == DEBUG_CAP                      # debugging invoked exactly CAP times
    assert stub_llm.test_gen_calls == 0                           # never reached test generation
    assert final.get("test_result") is None                       # never reached the test-run node
