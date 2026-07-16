"""Tests for the boundary/hardening changes from code review.

- work_items input is validated (fail fast, not a mid-loop crash).
- an executor/sandbox error during the gate becomes a gate FAILURE, not a graph crash.
"""

from collections.abc import Sequence

import pytest

from app.graph.nodes import gate_node, select_work_item_node
from app.graph.state import new_state
from app.integrations.executor import CheckResult, FakeExecutor, StrPath, set_executor


def test_new_state_rejects_non_list_work_items() -> None:
    with pytest.raises(ValueError):
        new_state(run_id="r", attempt=0, work_items="nope")  # type: ignore[arg-type]


def test_select_node_fails_fast_on_malformed_work_items() -> None:
    state = new_state(run_id="r", attempt=0)
    state["work_items"] = "not-a-list"  # type: ignore[typeddict-item]
    with pytest.raises(ValueError):
        select_work_item_node(state)


class _RaisingExecutor(FakeExecutor):
    """An executor whose completeness check blows up (simulates a sandbox timeout/partition)."""

    def files_complete(self, project_dir: StrPath, target_files: Sequence[str]) -> CheckResult:
        raise RuntimeError("sandbox down")


def test_gate_treats_executor_error_as_a_failure_not_a_crash() -> None:
    set_executor(_RaisingExecutor())
    try:
        out = gate_node(new_state(run_id="r", attempt=0, project_id="p"))
    finally:
        set_executor(None)

    gate = out["gate_result"]
    assert gate is not None
    assert gate["passed"] is False                       # error → gate fails, graph continues
    assert gate["checks"][0]["name"] == "files_complete"
    assert "executor error" in gate["checks"][0]["stderr"]
    assert "sandbox down" in gate["checks"][0]["stderr"]
