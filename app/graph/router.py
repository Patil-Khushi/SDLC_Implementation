"""Conditional routing for the IMP-001 subgraph.

The fixed gate IS the router source: these functions read state written by the deterministic
nodes and decide the next edge. The local repair cap is enforced here and is SEPARATE from the
orchestrator's ``attempt`` (which this service never touches).
"""

from __future__ import annotations

from app.graph.state import WorkflowState

#: Local repair cap — how many repair attempts a single work item gets before escalation.
REPAIR_CAP = 3


def route_after_select(state: WorkflowState) -> str:
    """After selecting: generate the next item, or auto-commit when the plan is exhausted.

    With human-in-the-loop removed, an exhausted plan goes straight to the single run-level
    commit — there is no batch-review approval and no rework queue.
    """
    if state.get("current_work_item") is None:
        return "commit"
    return "code_generator"


def route_after_codegen(state: WorkflowState) -> str:
    """After generation: run the gate on success, or escalate a failed item (no gate/commit).

    A generation failure (invalid model output after retry → no files) must NOT reach the gate
    or produce a commit; it is flagged as needs_human_review and ends the run.
    """
    return "gate" if state.get("codegen_ok", True) else "escalate"


def route_after_gate(state: WorkflowState) -> str:
    """The gate decision: all-pass → back to select (which auto-commits when done); fail under
    cap → repair; fail at cap → escalate (needs_human_review)."""
    gate_result = state.get("gate_result")
    if gate_result and gate_result.get("passed"):
        return "select"
    if int(state.get("repair_attempt", 0)) < REPAIR_CAP:
        return "repair"
    return "escalate"
