"""Shared LangGraph workflow state (the "clipboard", DEVELOPER_GUIDE.md §5A).

Each agent receives this state, updates ONLY the fields it owns, and returns it. It carries
both the guide's linear-pipeline fields (``review_report`` … one per downstream agent) and the
Code Generation agent's IMP-001 fields (``work_items``, ``gate_result``, ``repair_attempt`` …).

Two counters live here and must NOT be conflated (CLAUDE.md rule 3): ``repair_attempt`` is the
LOCAL per-work-item counter (reset to 0 on each new work item); ``attempt`` is the
ORCHESTRATOR's number, echoed back unchanged (this service never increments it).
"""

from typing import Any, TypedDict

from app.models import WorkItem


class GateCheck(TypedDict):
    """Result of one fixed-path check (compile/build/test/lint) against the current work item."""

    name: str        # "compile" | "build" | "test" | "lint"
    passed: bool
    stderr: str       # captured stderr (empty when passed) — the gate/router reads this
    exit_code: int


class GateResult(TypedDict):
    """Gate outcome: overall pass + per-check pass/fail + captured stderr (router source)."""

    passed: bool                # overall: every check in `checks` passed
    checks: list[GateCheck]     # per-check breakdown


class WorkflowState(TypedDict, total=False):
    # --- Identity / run metadata ---
    project_id: str
    run_id: str          # this service's id for the run
    attempt: int         # orchestrator's attempt number; echoed unchanged, never incremented here

    # --- Input ---
    # The design pack: a bundle of named artifacts (openapi.yaml, schema.sql,
    # validation-rules.json, routes.json, tokens.json, mockup.html, SKILL.md, ...) keyed by
    # name. Values are the artifact content (str) or parsed structures. Schema = 27 inputs, TBD.
    design_package: dict[str, Any]

    # --- Code Generation (IMP-001) internals ---
    work_items: list[WorkItem]            # design package decomposed into units of work
    work_item_index: int                  # graph cursor: index of the NEXT item to select
    current_work_item: WorkItem | None    # the item currently being generated
    generated_code: list[str]             # workspace-relative paths of files written this run
    codegen_ok: bool                      # did the current item's generation succeed (files written)?
    gate_result: GateResult | None        # most recent gate evaluation (pass/fail + stderr)
    repair_attempt: int                   # LOCAL repair counter, reset per work item
    generation_summary: str               # human-readable free-text summary of the run
    generation_metrics: dict[str, Any]    # run-level metrics (generation-metrics.json shape)

    # --- Downstream pipeline agent outputs (each agent writes only its own) ---
    review_report: str
    refactored_code: str
    unit_tests: str
    documentation: str
    security_report: str

    # --- Lifecycle ---
    workflow_status: str


def new_state(
    *,
    run_id: str,
    attempt: int,
    project_id: str = "",
    design_package: dict[str, Any] | None = None,
    work_items: list[WorkItem] | None = None,
) -> WorkflowState:
    """Build the initial state for a run.

    Identity + input + Code Generation internals get their starting values; downstream agents'
    output fields are left unset (each adds its own). ``repair_attempt`` starts at 0.

    Fails fast on a malformed ``work_items`` (must be a list) rather than crashing deep in the
    graph loop.
    """
    if work_items is not None and not isinstance(work_items, list):
        raise ValueError(f"work_items must be a list, got {type(work_items).__name__}")
    return {
        "project_id": project_id,
        "run_id": run_id,
        "attempt": attempt,
        "design_package": design_package or {},
        "work_items": work_items or [],
        "work_item_index": 0,
        "current_work_item": None,
        "generated_code": [],
        "gate_result": None,
        "repair_attempt": 0,
        "generation_summary": "",
        "generation_metrics": {},
        "workflow_status": "pending",
    }
