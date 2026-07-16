"""LangGraph node functions.

Each node wraps one step of the IMP-001 subgraph. Agents are instantiated once at import and
reused. The executor is resolved at run time via the provider (``get_executor``), so the same
node code works with the real MCP sandbox (set in the app lifespan) or a FakeExecutor (set in
tests).
"""

from __future__ import annotations

import logging

from langgraph.types import interrupt

from app.agents.code_generator import CodeGeneratorAgent
from app.graph.state import GateCheck, WorkflowState
from app.integrations.executor import get_executor
from app.services.boilerplate import render_scaffold

logger = logging.getLogger(__name__)

_code_generator = CodeGeneratorAgent()


def scaffold_node(state: WorkflowState) -> WorkflowState:
    """FIXED, deterministic: render the repo-root boilerplate once, before any work item.

    No LLM — Jinja2 templates only (app/services/boilerplate.py). Runs exactly once per run,
    so requirements.txt/package.json exist before the first work item's build check runs.
    """
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    files = render_scaffold(project_dir)
    generated = list(state.get("generated_code", []))
    written: list[str] = []
    for entry in files:
        path = f"{project_dir}/{entry['path']}"
        executor.write_file(path, entry["content"])
        written.append(path)
        generated.append(path)
    state["generated_code"] = generated
    names = ", ".join(w.rsplit("/", 1)[-1] for w in written)
    state["generation_summary"] = (
        state.get("generation_summary") or ""
    ) + f"[scaffold] rendered {len(written)} boilerplate file(s): {names}\n"
    return state


def code_generator_node(state: WorkflowState) -> WorkflowState:
    """LLM: generate + write files for the current work item (no gate/commit here)."""
    return _code_generator.execute(state)


def select_work_item_node(state: WorkflowState) -> WorkflowState:
    """Advance to the next unit of work; reset the LOCAL repair counter.

    Drains ``review_feedback`` (items a human rejected at batch_review) ONE AT A TIME before
    falling back to the normal work_items cursor — a drained item skips code_generator and goes
    straight to repair (route_after_select reads ``current_item_feedback`` for this). When both
    sources are exhausted, clears current_work_item so the run proceeds to batch_review.
    """
    feedback = dict(state.get("review_feedback") or {})
    while feedback:
        item_id, note = next(iter(feedback.items()))
        del feedback[item_id]
        work_item = next((wi for wi in state.get("work_items", []) if wi.id == item_id), None)
        if work_item is None:  # defensive: id no longer in the plan, skip it
            continue
        state["review_feedback"] = feedback
        state["current_work_item"] = work_item
        state["current_item_feedback"] = note
        state["repair_attempt"] = 0
        return state
    state["review_feedback"] = feedback  # now empty

    items = state.get("work_items", [])
    if not isinstance(items, list):  # fail fast on malformed input, don't crash mid-loop
        raise ValueError(f"work_items must be a list, got {type(items).__name__}")
    index = int(state.get("work_item_index", 0))
    state["current_item_feedback"] = ""
    if index < len(items):
        state["current_work_item"] = items[index]
        state["work_item_index"] = index + 1
        state["repair_attempt"] = 0  # LOCAL, reset per work item (never touches `attempt`)
    else:
        state["current_work_item"] = None  # plan (+ rework queue) exhausted -> batch_review
    return state


def gate_node(state: WorkflowState) -> WorkflowState:
    """FIXED, deterministic quality gate: ``files_complete`` ONLY.

    The gate's sole job is completeness — did the agent write every file this work item was told
    to produce (``target_files``)? It does NOT compile or build the code (that was dropped by
    design: generated source is committed on completeness + human approval, not on a green
    compiler). An executor error (timeout, sandbox/disk failure) is treated as a gate failure —
    recorded as a failing check — rather than crashing the graph. This node is the ROUTER source;
    it makes no routing decision itself.

    A failure here (a missing file) is routed through the repair/escalate path exactly as before:
    repair proposes the missing/fixed file, the gate re-checks. NOTE: ``compile``/``build``/
    ``test``/``lint`` remain on the Executor for later pipeline agents that own them, but are not
    part of this gate.
    """
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    work_item = state.get("current_work_item")
    target_files = work_item.target_files if work_item is not None else []
    checks: list[GateCheck] = []

    try:
        result = executor.files_complete(project_dir, target_files)
        checks.append({"name": result.name, "passed": result.passed, "stderr": result.stderr, "exit_code": result.exit_code})
    except Exception as exc:  # noqa: BLE001 - executor failure becomes a gate failure, not a crash
        logger.exception("gate: files_complete raised for run %s", state.get("run_id"))
        checks.append({"name": "files_complete", "passed": False, "stderr": f"executor error: {exc}", "exit_code": -1})

    state["gate_result"] = {"passed": bool(checks) and all(c["passed"] for c in checks), "checks": checks}
    return state


def commit_node(state: WorkflowState) -> WorkflowState:
    """FIXED: commit the WHOLE run's files in one commit. Reached ONLY after batch_review approval."""
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    work_items = state.get("work_items", [])
    files = state.get("generated_code", [])
    message = f"IMP-001 {state.get('run_id', 'run')}: {len(work_items)} work item(s), {len(files)} file(s)"
    try:
        executor.git_commit(project_dir, message)  # LLM never forms/executes this call (rule 2)
    except Exception as exc:  # noqa: BLE001 - don't crash the run on a commit failure
        logger.exception("commit failed for run %s", state.get("run_id"))
        state["generation_summary"] = (state.get("generation_summary") or "") + f"[commit] FAILED: {exc}\n"
        return state
    state["workflow_status"] = "completed"
    return state


def escalate_node(state: WorkflowState) -> WorkflowState:
    """Local repair cap reached: flag for human review (status persisted before the interrupt)."""
    state["workflow_status"] = "needs_human_review"
    return state


def human_review_node(state: WorkflowState) -> WorkflowState:
    """HITL pause on a REPAIR-CAP FAILURE. interrupt() suspends the run (needs a checkpointer).

    Distinct from ``batch_review_node``: this is the escalation-on-failure path and always ends
    the run (no resume-and-continue contract — see CLAUDE.md gap #7 for what's still undefined
    here).
    """
    interrupt({"reason": "needs_human_review", "run_id": state.get("run_id")})
    return state  # reached only after a human resumes the run


def batch_review_node(state: WorkflowState) -> WorkflowState:
    """All work items have gate-passed: persist ``workflow_status`` BEFORE the interrupt.

    A node that mutates state and then calls ``interrupt()`` in the same function loses that
    mutation for the pass that actually pauses (the function never reaches ``return`` on that
    pass, so nothing merges) — the same reason ``escalate_node`` is a separate, non-interrupting
    step before ``human_review_node``. This node is that step for the batch-review path; the
    interrupt itself lives in ``batch_review_wait_node``.
    """
    state["workflow_status"] = "pending_review"
    return state


def batch_review_wait_node(state: WorkflowState) -> WorkflowState:
    """HITL pause after ALL work items have gate-passed: one human decision for the whole run.

    interrupt() suspends the run; the resume value is
    ``{"approved": bool, "rejections": {item_id: feedback}}``.

    - Approved → ``workflow_status = "approved_for_commit"``; the router sends the run to the
      single, run-level ``commit`` node.
    - Rejected → ``review_feedback`` is populated with the named items; the router sends the run
      back to ``select``, which drains them one at a time through the existing repair path
      (skipping code_generator — the files already exist, only the fix content changes). Once
      drained and re-gate-passed, the run lands back here for another decision.
    """
    decision = interrupt(
        {
            "reason": "batch_review",
            "run_id": state.get("run_id"),
            "generation_summary": state.get("generation_summary", ""),
            "generated_code": state.get("generated_code", []),
            "work_items": [wi.id for wi in state.get("work_items", [])],
        }
    )
    if decision.get("approved"):
        state["workflow_status"] = "approved_for_commit"
        state["review_feedback"] = {}
    else:
        state["workflow_status"] = "in_rework"
        state["review_feedback"] = dict(decision.get("rejections") or {})
    return state
