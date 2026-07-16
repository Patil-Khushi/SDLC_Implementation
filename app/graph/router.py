"""Conditional routing for the IMP-001 subgraph.

The fixed gate IS the router source: these functions read state written by the deterministic
nodes and decide the next edge. The local repair cap is enforced here and is SEPARATE from the
orchestrator's ``attempt`` (which this service never touches).
"""

from __future__ import annotations

from langgraph.graph import END

from app.graph.state import WorkflowState

#: Local repair cap — how many repair attempts a single work item gets before escalation.
REPAIR_CAP = 3


def route_after_select(state: WorkflowState) -> str:
    """After selecting: generate a fresh item, repair a reworked item, or batch-review when done.

    A picked item carrying ``current_item_feedback`` came from the rework queue (a human
    rejection at batch_review) — it skips code_generator and goes straight to repair, since the
    files already exist and only the fix content needs to change.
    """
    if state.get("current_work_item") is None:
        return "batch_review"
    return "repair" if state.get("current_item_feedback") else "code_generator"


def route_after_codegen(state: WorkflowState) -> str:
    """After generation: run the gate on success, or escalate a failed item (no gate/commit).

    A generation failure (invalid model output after retry → no files) must NOT reach the gate
    or produce a commit; it is flagged for human review.
    """
    return "gate" if state.get("codegen_ok", True) else "escalate"


def route_after_gate(state: WorkflowState) -> str:
    """The gate decision: all-pass → back to select (no per-item commit); fail under cap →
    repair; fail at cap → escalate. Commits are deferred to batch_review approval."""
    gate_result = state.get("gate_result")
    if gate_result and gate_result.get("passed"):
        return "select"
    if int(state.get("repair_attempt", 0)) < REPAIR_CAP:
        return "repair"
    return "escalate"


def route_after_batch_review(state: WorkflowState) -> str:
    """Approved → the single run-level commit; rejected → drain review_feedback via select/repair."""
    return "commit" if state.get("workflow_status") == "approved_for_commit" else "select"
