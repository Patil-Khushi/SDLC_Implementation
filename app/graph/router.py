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


def route_entry(state: WorkflowState) -> str:
    """Which lane a run enters: build a new app, or change an existing one.

    The ONE place the two modes diverge. Anything that is not explicitly ``"brownfield"`` — an
    absent field, an empty string, a legacy checkpoint written before this field existed — takes
    the greenfield lane, so every existing caller is byte-identical.

    An explicit field rather than the key-presence trick ``route_after_refactoring`` uses: this
    decision determines whether ``scaffold_node`` runs, and ``scaffold_node`` against a clone
    overwrites the repo's manifests and fast-forwards them onto its default branch. A decision
    with that blast radius has to be readable in a checkpoint, not inferred.
    """
    return "acquire" if state.get("source_mode") == "brownfield" else "scaffold"


def route_after_acquire(state: WorkflowState) -> str:
    """After acquiring a repo: plan the change, or escalate a failed acquisition.

    Routes on the PRODUCT of acquisition (an inventory) rather than on a status string: the
    inventory is what the planner needs, so "is there one?" is the question that actually matters,
    and it cannot drift the way a status label can.
    """
    return "change_plan" if state.get("repo_inventory") else "escalate"


def route_after_change_plan(state: WorkflowState) -> str:
    """After planning: implement the plan, or escalate when there isn't one.

    ``work_items`` is empty for all three failure shapes — the planner refused as out-of-scope, its
    reply was unusable, or the validator rejected the plan — and populated only when a plan
    survived validation. So this one check covers every way planning can fail to produce work.
    """
    return "select" if state.get("work_items") else "escalate"


def route_after_modify(state: WorkflowState) -> str:
    """After an edit attempt: verify it, or escalate an item the model declined to implement.

    Mirrors ``route_after_codegen``. ``codegen_ok`` is False when the modifier wrote nothing —
    which the change gate would fail anyway, but escalating here reports the model's OWN reason
    ("the code does not do what the work item assumes") instead of the gate's symptom
    ("no change detected").
    """
    return "change_gate" if state.get("codegen_ok", True) else "escalate"


def route_after_change_gate(state: WorkflowState) -> str:
    """After verifying an edit: next item, retry, or escalate at the cap.

    Deliberately the same shape and the same ``REPAIR_CAP`` as ``route_after_gate``. A retry goes
    back to the MODIFIER (not to a separate repair agent): the gate's failure text — "no change
    detected in X", "file(s) changed that no work item claimed" — is written to be actionable by
    the same agent that made the edit, and a second agent would have to re-read everything the
    first one just read.
    """
    result = state.get("gate_result")
    if result and result.get("passed"):
        return "select"
    # Pure, like every other router here: the counter is incremented by the node that retries
    # (CodeModifierAgent on re-entry), exactly as repair_node owns it on the greenfield lane.
    return "code_modifier" if int(state.get("repair_attempt", 0)) < REPAIR_CAP else "escalate"


def route_after_select(state: WorkflowState) -> str:
    """After selecting: work the next item, or wrap up when the plan is exhausted.

    With human-in-the-loop removed, an exhausted plan goes straight to the commit step — there is
    no batch-review approval and no rework queue.

    Both lanes share ``select`` (the cursor walk over ``work_items`` is identical) and diverge
    here, because what "work an item" means is the whole difference between the modes: greenfield
    GENERATES a file that does not exist, brownfield EDITS one that does.
    """
    brownfield = state.get("source_mode") == "brownfield"
    if state.get("current_work_item") is None:
        return "change_commit" if brownfield else "commit"
    return "code_modifier" if brownfield else "code_generator"


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


def route_after_change_verify(state: WorkflowState) -> str:
    """After re-running the repository's own tests: commit, or stop on a regression.

    Only ``regressed`` blocks — the suite passed before this change and fails now. Everything else
    proceeds to a DRAFT pull request a human must review:

    * ``preexisting`` — it was already failing; blaming the change would make every repo with a red
      suite un-changeable.
    * ``unverified`` — the suite could not run (missing dev dependency, no tests). Nothing was
      proven, which is not the same as something being wrong. The report and the PR both say so
      plainly, so the reviewer knows what they are and are not being handed.
    """
    return "escalate" if state.get("verify_verdict") == "regressed" else "change_commit"
