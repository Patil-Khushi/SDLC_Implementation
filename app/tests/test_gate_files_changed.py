"""The gate check that makes ``files_complete`` non-vacuous for an edit-in-place item.

``files_complete`` asks whether the target exists. For a create item that is the right question and
the only one the gate needs. For a ``modify`` item the file already exists, so the gate passes even
when the agent wrote nothing — the run then commits, pushes, and reports an applied change that
never happened. That is the worst failure available to a conversational loop, because the user is
told their request landed.

The baseline is taken in ``select_work_item_node``, not at the gate, because by gate time the edit
has happened and there is nothing left to compare against.
"""

from __future__ import annotations

import pytest

from app.graph import nodes
from app.graph.state import WorkflowState, new_state
from app.integrations.executor import FakeExecutor, set_executor
from app.models import WorkItem

PROJECT = "p1"


@pytest.fixture
def executor():
    ex = FakeExecutor(files={f"{PROJECT}/backend/login.py": "def login(password):\n    ...\n"})
    set_executor(ex)
    yield ex
    set_executor(None)


def _state(item: WorkItem) -> WorkflowState:
    state = new_state(run_id="r", attempt=0, project_id=PROJECT)
    state["work_items"] = [item]
    state["work_item_index"] = 0
    return state


_MODIFY = WorkItem(
    id="WI-001-m1", target_files=["backend/login.py"], action="modify",
    change_intent="use a one-time password",
)
_CREATE = WorkItem(id="WI-002", target_files=["backend/login.py"])


def test_select_snapshots_the_targets_of_a_modify_item(executor):
    out = nodes.select_work_item_node(_state(_MODIFY))
    assert list(out["pre_item_digests"]) == ["backend/login.py"]
    assert out["current_work_item"].id == "WI-001-m1"


def test_select_does_not_snapshot_for_a_create_item(executor):
    # Hashing files that do not exist yet is pure cost, and files_complete already suffices.
    out = nodes.select_work_item_node(_state(_CREATE))
    assert out["pre_item_digests"] == {}


def test_a_real_edit_passes_the_gate(executor):
    state = nodes.select_work_item_node(_state(_MODIFY))
    executor.write_file(f"{PROJECT}/backend/login.py", "def login(otp):\n    ...\n")

    out = nodes.gate_node(state)

    assert out["gate_result"]["passed"] is True
    names = [c["name"] for c in out["gate_result"]["checks"]]
    assert names == ["files_complete", "files_changed"]
    changed = next(c for c in out["gate_result"]["checks"] if c["name"] == "files_changed")
    assert changed["stdout"] == "backend/login.py"


def test_a_noop_edit_FAILS_the_gate_even_though_the_file_exists(executor):
    # The whole point. files_complete alone would pass this.
    state = nodes.select_work_item_node(_state(_MODIFY))

    out = nodes.gate_node(state)

    assert out["gate_result"]["passed"] is False
    complete = next(c for c in out["gate_result"]["checks"] if c["name"] == "files_complete")
    changed = next(c for c in out["gate_result"]["checks"] if c["name"] == "files_changed")
    assert complete["passed"] is True, "the file exists, so completeness is satisfied"
    assert changed["passed"] is False, "but nothing moved"
    # The stderr is the repair agent's instruction, so it must name the item and the request.
    assert "WI-001-m1" in changed["stderr"]
    assert "backend/login.py" in changed["stderr"]
    assert "one-time password" in changed["stderr"]


def test_a_create_item_gets_only_the_completeness_check(executor):
    # Backward compatibility: the create path's gate is untouched.
    state = nodes.select_work_item_node(_state(_CREATE))
    out = nodes.gate_node(state)
    assert [c["name"] for c in out["gate_result"]["checks"]] == ["files_complete"]
    assert out["gate_result"]["passed"] is True


def test_a_delete_item_is_also_proven_to_have_changed_something(executor):
    item = _MODIFY.model_copy(update={"action": "delete", "change_intent": "remove login"})
    state = nodes.select_work_item_node(_state(item))
    out = nodes.gate_node(state)
    assert out["gate_result"]["passed"] is False

    executor.write_file(f"{PROJECT}/backend/login.py", "# removed\n")
    out = nodes.gate_node(state)
    assert out["gate_result"]["passed"] is True


def test_a_missing_baseline_reads_as_changed_rather_than_blocking(executor):
    # If select could not snapshot (an executor hiccup), an existing-and-readable target counts as
    # changed: a create-shaped absence in `before` is treated as "it moved". Better to let the run
    # proceed than to hard-block a correct edit on a lost baseline.
    state = _state(_MODIFY)
    state["current_work_item"] = _MODIFY
    state["pre_item_digests"] = {}
    out = nodes.gate_node(state)
    assert out["gate_result"]["passed"] is True


def test_an_unreadable_target_is_a_gate_failure_not_a_crash(executor):
    item = WorkItem(id="WI-9", target_files=["backend/gone.py"], action="modify",
                    change_intent="x")
    state = _state(item)
    state["current_work_item"] = item
    state["pre_item_digests"] = {"backend/gone.py": "deadbeef"}

    out = nodes.gate_node(state)

    assert out["gate_result"]["passed"] is False
    changed = next(c for c in out["gate_result"]["checks"] if c["name"] == "files_changed")
    assert changed["passed"] is False
