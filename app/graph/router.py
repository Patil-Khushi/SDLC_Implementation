"""Conditional routing for the IMP-001 subgraph.

The fixed gate IS the router source: these functions read state written by the deterministic
nodes and decide the next edge. The local repair cap is enforced here and is SEPARATE from the
orchestrator's ``attempt`` (which this service never touches).
"""

from __future__ import annotations

from app.graph.state import WorkflowState

#: Local repair cap — how many repair attempts a single work item gets before escalation.
REPAIR_CAP = 3

#: Local cap for the separate post-commit Debugging<->Unit-Test loop. This is NOT the same counter
#: or cap as REPAIR_CAP — that one belongs to the earlier per-work-item code-generation loop and is
#: already spent by the time this phase runs.
#:
#: This caps CONSECUTIVE NO-PROGRESS rounds, not total rounds: ``debug_attempt`` resets to 0
#: whenever a round actually reduces the failure count (see app/agents/debugging.py). It was first
#: raised 3 -> 10 while still counting raw entries, which only postponed the real problem — on a
#: large generated project (259 test files across 59 independently generated work items) the
#: post-commit failures are a long tail of small INDEPENDENT issues, so *any* flat per-entry cap
#: cuts off a loop that is still converging, while rounds that fixed nothing cost the same as
#: rounds that fixed several files. 10 consecutive stalled rounds is a genuinely stuck loop.
#: ``debugging.DEBUG_ROUNDS_CEILING`` separately bounds TOTAL rounds so an oscillating loop (each
#: fix breaking something else, so "progress" keeps resetting this counter) still terminates.
DEBUG_CAP = 10

#: Local cap for the Security<->Refactoring loop, at the very end of the run. Separate counter
#: (``security_loop_attempt``) and separate cap from REPAIR_CAP/DEBUG_CAP above — this loop starts
#: only after Code Gen, Debugging, and Unit Test have already finished, and reuses the SAME
#: Refactoring agent/node Code Review's one-shot call uses (see ``route_after_refactoring``).
SECURITY_LOOP_CAP = 3


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


def _debug_budget_left(state: WorkflowState) -> bool:
    """True while the debug/test loop may run another round.

    Two independent limits, both required: the progress-sensitive stall counter (``debug_attempt``
    < DEBUG_CAP) and the absolute total-rounds backstop (``debug_rounds`` < DEBUG_ROUNDS_CEILING).
    Neither subsumes the other — the first lets a converging loop keep going, the second stops an
    oscillating one that would otherwise reset the first forever.
    """
    from app.agents.debugging import DEBUG_ROUNDS_CEILING  # local: avoids a circular import

    return (
        int(state.get("debug_attempt", 0)) < DEBUG_CAP
        and int(state.get("debug_rounds", 0)) < DEBUG_ROUNDS_CEILING
    )


def route_after_debug_check(state: WorkflowState) -> str:
    """The debug-check decision: passing → run existing tests if any were already generated in a
    prior pass, else generate them for the first time; fail with budget left → debugging; fail with
    the budget spent → escalate (needs_human_review)."""
    debug_result = state.get("debug_result")
    if debug_result and debug_result.get("passed"):
        return "unit_test_run" if state.get("unit_tests") else "unit_test_generate"
    if _debug_budget_left(state):
        return "debugging"
    return "escalate"


def route_after_test_generate(state: WorkflowState) -> str:
    """After test generation: run the tests on success, or escalate a failed generation (no test
    run)."""
    return "unit_test_run" if state.get("tests_ok", True) else "escalate"


def route_after_test_run(state: WorkflowState) -> str:
    """The test-run decision: all-pass → done (the graph maps this to ``debug_publish`` — which
    commits/pushes the loop's fixes + tests to 'dev' — then Documentation/Security/finalize still
    run; NOT the real END sentinel); fail under cap → debugging; fail at cap → escalate
    (needs_human_review)."""
    test_result = state.get("test_result")
    if test_result and test_result.get("passed"):
        return "done"
    if _debug_budget_left(state):
        return "debugging"
    return "escalate"


def route_after_security(state: WorkflowState) -> str:
    """The run's decision after a scan: approved → finalize (open the dev -> main PR, then package
    the zip output); changes_requested under the loop cap → refactoring (fixes Security's findings,
    then loops back here to re-scan); changes_requested at the cap → escalate (needs_human_review,
    no PR/zip) — the same terminal path a repair/debug cap-out uses."""
    if state.get("security_verdict") == "approve":
        return "finalize"
    if int(state.get("security_loop_attempt", 0)) < SECURITY_LOOP_CAP:
        return "refactoring"
    return "escalate"


def route_after_refactoring(state: WorkflowState) -> str:
    """Refactoring is shared by two callers: Code Review's one-shot call (on the way to the
    debug/test loop) and the Security<->Refactoring loop (repeated, capped). ``security_verdict``
    is written only once Security has actually run — its presence on state is exactly the signal
    that this call is a security-loop re-entry, not the original code-review-triggered one."""
    return "security" if "security_verdict" in state else "debug_check"
