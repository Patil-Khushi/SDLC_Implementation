"""WorkflowState carries the guide pipeline fields + IMP-001 internals."""

from app.graph.state import GateResult, WorkflowState, new_state
from app.models import WorkItem

ALL_FIELDS = {
    "project_id", "run_id", "attempt", "design_package", "repo_url", "branch", "commit_sha",
    "work_items", "work_item_index", "current_work_item", "generated_code", "scaffold_files",
    "codegen_ok", "gate_result", "repair_attempt", "generation_summary", "generation_metrics",
    "push_enabled", "git_remote", "git_token",
    "review_report", "review_report_path", "review_findings_path", "refactored_code", "unit_tests",
    "documentation", "security_report", "workflow_status",
}

INITIALIZED_FIELDS = {
    "project_id", "run_id", "attempt", "design_package",
    "work_items", "work_item_index", "current_work_item", "generated_code", "scaffold_files",
    "gate_result", "repair_attempt", "generation_summary", "generation_metrics",
    "push_enabled", "git_remote", "git_token", "workflow_status",
}


def test_workflowstate_declares_all_fields() -> None:
    assert set(WorkflowState.__annotations__.keys()) == ALL_FIELDS


def test_new_state_defaults() -> None:
    state = new_state(run_id="r1", attempt=3, project_id="p1", design_package={"openapi.yaml": "..."})
    assert set(state.keys()) == INITIALIZED_FIELDS
    assert state["design_package"] == {"openapi.yaml": "..."}   # design pack is an artifact bundle
    assert state["run_id"] == "r1"
    assert state["attempt"] == 3                # orchestrator's counter, echoed unchanged
    assert state["repair_attempt"] == 0         # local counter starts at zero
    assert state["generated_code"] == []        # list of file paths, not a str
    assert state["work_items"] == []
    assert state["current_work_item"] is None
    assert state["gate_result"] is None
    assert state["generation_metrics"] == {}
    assert state["workflow_status"] == "pending"
    assert state["push_enabled"] is False        # push OFF by default (opt-in)
    assert state["git_remote"] == "" and state["git_token"] == ""


def test_new_state_accepts_push_config() -> None:
    state = new_state(run_id="r", attempt=0, push_enabled=True, git_remote="me/app", git_token="tok")
    assert state["push_enabled"] is True
    assert state["git_remote"] == "me/app"
    assert state["git_token"] == "tok"


def test_new_state_leaves_downstream_outputs_unset() -> None:
    state = new_state(run_id="r", attempt=0)
    for field in ("review_report", "refactored_code", "unit_tests", "documentation", "security_report"):
        assert field not in state


def test_generated_code_is_a_list() -> None:
    state = new_state(run_id="r", attempt=0)
    state["generated_code"].append("p1/generated/main.py")
    assert state["generated_code"] == ["p1/generated/main.py"]


def test_work_items_hold_workitem_models() -> None:
    item = WorkItem(id="WI-1", target_files=["app/api/login.py"])
    state = new_state(run_id="r", attempt=0)
    state["work_items"] = [item]
    assert state["work_items"][0].id == "WI-1"


def test_gate_result_shape() -> None:
    gate_result: GateResult = {
        "passed": False,
        "checks": [{"name": "compile", "passed": False, "stderr": "SyntaxError", "exit_code": 1}],
    }
    assert gate_result["checks"][0]["stderr"] == "SyntaxError"
