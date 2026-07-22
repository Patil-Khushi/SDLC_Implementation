"""Conditional routing for the IMP-001 subgraph.

The fixed gate IS the router source: these functions read state written by the deterministic
nodes and decide the next edge. The local repair cap is enforced here and is SEPARATE from the
orchestrator's ``attempt`` (which this service never touches).
"""

from __future__ import annotations

from app.graph.state import WorkflowState

#: Local repair cap — how many repair attempts a single work item gets before escalation.
REPAIR_CAP = 3

#: Local retry cap for the separate post-commit Debugging<->Unit-Test loop. This is NOT the
#: same counter or cap as REPAIR_CAP — that one belongs to the earlier per-work-item
#: code-generation loop and is already spent by the time this phase runs.
DEBUG_CAP = 3


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


def route_after_debug_check(state: WorkflowState) -> str:
    """The debug-check decision: passing → run existing tests if any were already generated in a
    prior pass, else generate them for the first time; fail under cap → debugging; fail at cap →
    escalate (needs_human_review)."""
    debug_result = state.get("debug_result")
    if debug_result and debug_result.get("passed"):
        return "unit_test_run" if state.get("unit_tests") else "unit_test_generate"
    if int(state.get("debug_attempt", 0)) < DEBUG_CAP:
        return "debugging"
    return "escalate"


def route_after_test_generate(state: WorkflowState) -> str:
    """After test generation: run the tests on success, or escalate a failed generation (no test
    run)."""
    return "unit_test_run" if state.get("tests_ok", True) else "escalate"


def route_after_test_run(state: WorkflowState) -> str:
    """The test-run decision: all-pass → done (the graph maps this string to the real END
    sentinel, not a node name); fail under cap → debugging; fail at cap → escalate
    (needs_human_review)."""
    test_result = state.get("test_result")
    if test_result and test_result.get("passed"):
        return "done"
    if int(state.get("debug_attempt", 0)) < DEBUG_CAP:
        return "debugging"
    return "escalate"


def route_after_security(state: WorkflowState) -> str:
    """The run's final decision: Security approved → finalize (open the dev -> main PR);
    changes_requested → escalate (needs_human_review, no PR opened) — same terminal path a
    repair/debug cap-out uses. No automated fix-it loop here: that's `main`'s Code-Review-driven
    Refactoring stage's job, upstream of Security, not this one's."""
    if state.get("security_verdict") == "approve":
        return "finalize"
    return "escalate"
