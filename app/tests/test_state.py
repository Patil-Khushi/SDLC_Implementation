"""WorkflowState carries the guide pipeline fields + IMP-001 internals."""

from app.graph.state import GateResult, WorkflowState, new_state
from app.models import WorkItem

ALL_FIELDS = {
    "project_id", "run_id", "attempt", "design_package", "repo_url", "branch", "commit_sha",
    # pack preflight: findings + canonical facts computed by scaffold_node (never seeded)
    "pack_findings", "pack_facts", "pack_preflight_report", "pack_preflight_report_path",
    "base_branch",
    # brownfield: an existing codebase + a change request, instead of a design pack
    "source_mode", "source_repo_url", "change_request", "base_ref",
    "base_sha", "baseline_digests", "repo_inventory",
    "change_plan_notes", "change_plan_errors", "change_impacts",
    "changed_files", "modifier_notes", "change_report", "change_report_path",
    "test_command", "baseline_test", "verify_test", "verify_verdict",
    "work_items", "work_item_index", "current_work_item", "generated_code", "scaffold_files",
    "codegen_ok", "gate_result", "repair_attempt", "debug_attempt", "debug_last_failure_count",
    "debug_last_failure_kind", "debug_rounds", "debug_result", "debug_report", "debug_report_path",
    "tests_ok", "test_result", "unit_test_report", "unit_test_report_path",
    "generation_summary", "generation_metrics",
    "push_enabled", "git_remote", "git_token",
    "review_report", "review_report_path", "review_findings_path", "refactored_code",
    "refactored_files", "refactoring_report", "refactoring_report_path",
    "unresolved_imports", "unit_tests",
    "unit_test_report", "unit_test_report_path",
    "debugging_report", "debugging_report_path",
    "documentation", "security_report", "security_report_path", "security_verdict",
    "security_findings_path", "security_loop_attempt", "pr_url", "finalize_status", "package_path",
    # conversational code generation: a chat-driven patch to the plan, applied between work items
    "chat_enabled", "chat_seq", "chat_pending", "chat_patch", "chat_planner_notes", "chat_log",
    "chat_turns", "requirement_ledger", "pre_item_digests", "orphan_files",
    "workflow_status",
}

INITIALIZED_FIELDS = {
    "project_id", "run_id", "attempt", "design_package",
    "work_items", "work_item_index", "current_work_item", "generated_code", "scaffold_files",
    "gate_result", "repair_attempt", "debug_attempt", "debug_last_failure_count",
    "debug_last_failure_kind", "debug_rounds",
    "debug_result", "generation_summary",
    "generation_metrics",
    "push_enabled", "git_remote", "git_token", "security_loop_attempt", "workflow_status",
    # brownfield INPUTS are always seeded (defaulting to greenfield) so route_entry reads a real
    # value rather than an absent key — the mode decision is never left implicit.
    "source_mode", "source_repo_url", "change_request", "base_ref",
    # conversational code generation is seeded the same way, and for the same reason: chat_intake
    # reads a real chat_enabled (False) rather than an absent key, so the pass-through is explicit.
    "chat_enabled", "chat_seq", "chat_pending", "chat_patch", "chat_planner_notes", "chat_log",
    "chat_turns", "requirement_ledger", "pre_item_digests", "orphan_files",
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
    assert state["debug_attempt"] == 0
    assert state["security_loop_attempt"] == 0  # local counter for the Security<->Refactoring loop
    assert state["debug_result"] is None
    assert state["generated_code"] == []        # list of file paths, not a str
    assert state["work_items"] == []
    assert state["current_work_item"] is None
    assert state["gate_result"] is None
    assert state["generation_metrics"] == {}
    assert state["workflow_status"] == "pending"
    assert state["push_enabled"] is False        # push OFF by default (opt-in)
    assert state["git_remote"] == "" and state["git_token"] == ""
    # Chat OFF by default: this is what keeps a run byte-identical to before the feature existed.
    assert state["chat_enabled"] is False
    assert state["chat_seq"] == 0 and state["chat_turns"] == 0
    assert state["chat_pending"] == [] and state["chat_log"] == []
    assert state["chat_patch"] == {} and state["chat_planner_notes"] == ""
    assert state["requirement_ledger"] == []
    assert state["pre_item_digests"] == {} and state["orphan_files"] == []


def test_new_state_can_opt_into_chat() -> None:
    state = new_state(run_id="r", attempt=0, chat_enabled=True)
    assert state["chat_enabled"] is True


def test_new_state_accepts_push_config() -> None:
    state = new_state(run_id="r", attempt=0, push_enabled=True, git_remote="me/app", git_token="tok")
    assert state["push_enabled"] is True
    assert state["git_remote"] == "me/app"
    assert state["git_token"] == "tok"


def test_new_state_leaves_downstream_outputs_unset() -> None:
    state = new_state(run_id="r", attempt=0)
    for field in ("review_report", "refactored_code", "refactored_files", "refactoring_report",
                  "refactoring_report_path", "unit_tests", "documentation", "security_report",
                  "security_report_path", "security_verdict", "security_findings_path",
                  "pr_url", "finalize_status", "package_path", "tests_ok", "test_result"):
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
