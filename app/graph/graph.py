"""LangGraph workflow definition — the IMP-001 code-generation subgraph.

Renders the boilerplate scaffold once, then loops over the plan's work items: generate → fixed
gate (files_complete ONLY — did it write every target file? no compile/build) → back to select
(no per-item commit) | repair→gate | escalate (failure). Once the plan is exhausted, the run
auto-commits (one run-level commit), then the post-commit pipeline runs in this order: Code
Review (clone the committed repo, run static analysis, write the report) → Refactoring (apply the
fixes the review named, writing corrected files back to the shared exec-sandbox + a refactoring
report) → Refactoring Publish (FIXED commit + push of the edited files to 'dev', so the Debugging
agent — or Security, on a re-scan — can pull the refactored code from the remote) → a
Debugging<->Unit-Test loop (compile/build check → generate/run unit tests on the refactored code,
with an LLM debugging repair path on failure) → Debug Publish (FIXED commit + push of the debug
fixes + generated unit tests to 'dev', so Security's re-scan and the eventual PR include the tested
code and the tests themselves) → Documentation (writes a README from the final source) → Security
(clones the repo again, runs Semgrep, writes a verdict). NO human approval step anywhere. The fixed
gate/check nodes (and Security's verdict) are the router source; the local repair/debug caps live
in router.py.

Security's verdict drives what happens next (``route_after_security``): ``approve`` →
``finalize`` opens (or finds) a `dev -> main` pull request — it never auto-merges, a human
approves that on GitHub — then ``package`` zips the generated project + README/review/security
reports into one downloadable artifact. ``changes_requested`` under ``router.SECURITY_LOOP_CAP``
→ ``refactoring`` + ``refactoring_publish`` (the SAME nodes Code Review's one-shot call uses —
see ``route_after_refactoring`` and ``RefactoringAgent``'s class docstring for how it tells the
two callers apart) fixes exactly the findings Security named, publishes them to `dev`, then loops
back to ``security`` to re-scan. At the cap, still failing → ``escalate`` (``needs_human_review``,
no PR/zip) — the same terminal path a repair/debug cap-out already uses.

    scaffold → select → code_generator → gate ─┬─ pass ──────────────→ select (loop)
                  ▲                             ├─ fail, repair<CAP ─→ repair → gate
                  │                             └─ fail, repair>=CAP → escalate → END
                  │                                                    (needs_human_review)
                  └── select: nothing left → commit
                                                  │
                                                  ▼
                            code_review → refactoring → refactoring_publish ─┬─ (from code_review) → debug_check
                                                                              └─ (from security loop) → security
                                             debug_check ─┬─ pass, no tests yet ─→ unit_test_generate
                                                           ├─ pass, tests exist ──→ unit_test_run
                                                           ├─ fail, debug<CAP ────→ debugging → debug_check
                                                           └─ fail, debug>=CAP ───→ escalate → END
                                             unit_test_generate ─┬─ ok ──→ unit_test_run
                                                                 └─ fail → escalate → END
                                             unit_test_run ─┬─ pass ──→ debug_publish → documentation → security
                                                             ├─ fail, debug<CAP ─→ debugging → debug_check
                                                             └─ fail, debug>=CAP → escalate → END
                                             security ─┬─ verdict=approve ────────────→ finalize → package → END
                                                        ├─ changes_requested, loop<CAP → refactoring → security (loop)
                                                        └─ changes_requested, loop>=CAP → escalate → END

Code Review + Refactoring run at least ONCE, right after the run-level commit and BEFORE the
debug/test loop, so the loop verifies the refactored (and published) code; Refactoring may run up
to ``SECURITY_LOOP_CAP`` MORE times later, driven by Security instead — each of those passes
publishes too, so Security's re-scan (a fresh clone) actually sees the fix. Every escalate branch
in the code-generation loop above bypasses the whole post-commit pipeline. The Refactoring AGENT
never forms a git call (rule 2) and runs no gate — the fixed ``refactoring_publish`` node right
after it commits the edited files and pushes `dev` (a no-op when nothing was edited). Symmetrically,
the Debugging/Unit-Test agents never commit either — the fixed ``debug_publish`` node on the
``unit_test_run`` pass edge is what persists their output (debug fixes + the generated unit tests)
to `dev`, so Security's re-scan and finalize's PR carry the tested code and the tests. Documentation/
Security/finalize all degrade gracefully on a missing ``repo_url`` or a GitHub API hiccup rather
than crashing the run; ``package`` runs even if ``finalize``'s PR call failed, so a GitHub hiccup
never withholds the tangible zip output. The run's true terminal ``workflow_status`` is set by
``package`` (approve path) or ``escalate`` (changes_requested path) — Unit Testing's earlier
"completed" stamp is just an intermediate marker.

Human-in-the-loop was removed as not required: the batch-review approval interrupt (and its
rework loop) is gone — a completed plan commits automatically. The escalation path still flags
``needs_human_review`` for the orchestrator, but no longer pauses on an interrupt (it had no
resume contract and always ended the run anyway).

Compiled with a checkpointer so ``get_state`` (used by the API/demo to read a finished run) works;
the graph itself no longer contains any interrupt().
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agents.debugging import debugging_node
from app.agents.refactoring import refactoring_node
from app.agents.repair import repair_node
from app.graph import nodes
from app.graph.router import (
    route_after_codegen,
    route_after_debug_check,
    route_after_gate,
    route_after_refactoring,
    route_after_security,
    route_after_select,
    route_after_test_generate,
    route_after_test_run,
)
from app.graph.state import WorkflowState


def build_graph():
    """Build and compile the IMP-001 workflow graph."""
    graph = StateGraph(WorkflowState)

    graph.add_node("scaffold", nodes.scaffold_node)
    graph.add_node("select", nodes.select_work_item_node)
    graph.add_node("code_generator", nodes.code_generator_node)
    graph.add_node("gate", nodes.gate_node)
    graph.add_node("feature_publish", nodes.feature_publish_node)
    graph.add_node("reconcile", nodes.reconcile_node)
    graph.add_node("commit", nodes.commit_node)
    graph.add_node("repair", repair_node)
    graph.add_node("escalate", nodes.escalate_node)
    graph.add_node("debug_check", nodes.debug_check_node)
    graph.add_node("debugging", debugging_node)
    graph.add_node("unit_test_generate", nodes.unit_test_generate_node)
    graph.add_node("unit_test_run", nodes.unit_test_run_node)
    # FIXED publish of the Debugging<->Unit-Test loop's output (debug fixes + generated tests) to
    # 'dev' — the debug/test analogue of refactoring_publish, on the unit_test_run pass edge.
    graph.add_node("debug_publish", nodes.debug_publish_node)
    graph.add_node("code_review", nodes.code_review_node)
    graph.add_node("refactoring", refactoring_node)
    graph.add_node("refactoring_publish", nodes.refactoring_publish_node)
    graph.add_node("documentation", nodes.documentation_node)
    graph.add_node("security", nodes.security_node)
    graph.add_node("finalize", nodes.finalize_node)
    graph.add_node("package", nodes.package_node)

    graph.add_edge(START, "scaffold")
    graph.add_edge("scaffold", "select")
    graph.add_conditional_edges(
        "select",
        route_after_select,
        {"code_generator": "code_generator", "commit": "reconcile"},
    )
    graph.add_edge("reconcile", "commit")  # deterministic wiring pass, then the run-level commit
    graph.add_conditional_edges(
        "code_generator", route_after_codegen, {"gate": "gate", "escalate": "escalate"}
    )
    # On a gate PASS the router returns "select"; route it THROUGH feature_publish first so the
    # just-completed feature is committed+pushed to 'dev' live (incremental publish), then advance.
    graph.add_conditional_edges(
        "gate", route_after_gate,
        {"select": "feature_publish", "repair": "repair", "escalate": "escalate"},
    )
    graph.add_edge("feature_publish", "select")  # per-feature live push done → select next item
    graph.add_edge("commit", "code_review")  # run-level commit/finalize → Code Review runs first
    graph.add_edge("repair", "gate")          # repair → back to the fixed gate
    graph.add_edge("escalate", END)           # failure flagged (needs_human_review) → done, no pause
    graph.add_conditional_edges(
        "debug_check",
        route_after_debug_check,
        {
            "unit_test_generate": "unit_test_generate",
            "unit_test_run": "unit_test_run",
            "debugging": "debugging",
            "escalate": "escalate",
        },
    )
    graph.add_conditional_edges(
        "unit_test_generate",
        route_after_test_generate,
        {"unit_test_run": "unit_test_run", "escalate": "escalate"},
    )
    # A passing test run routes to debug_publish (persist the loop's output to 'dev'), THEN on to
    # Documentation/Security/finalize/package — NOT straight to END.
    graph.add_conditional_edges(
        "unit_test_run",
        route_after_test_run,
        {"done": "debug_publish", "debugging": "debugging", "escalate": "escalate"},
    )
    graph.add_edge("debug_publish", "documentation")  # debug/test output published → document it
    graph.add_edge("debugging", "debug_check")    # debugging → back to the fixed debug/build check
    graph.add_edge("code_review", "refactoring")  # review written → apply the fixes it named
    # fixes applied → FIXED commit+push of the edited files to 'dev' (rule 2; no-op when nothing
    # was edited), for BOTH callers — Code Review's one-shot pass and each Security-loop pass —
    # so downstream verification (debug/test loop, or Security's re-scan) sees the real fix.
    graph.add_edge("refactoring", "refactoring_publish")
    graph.add_conditional_edges(
        "refactoring_publish", route_after_refactoring,
        {"debug_check": "debug_check", "security": "security"},
    )
    graph.add_edge("documentation", "security")
    graph.add_conditional_edges(
        "security", route_after_security,
        {"finalize": "finalize", "refactoring": "refactoring", "escalate": "escalate"},
    )
    graph.add_edge("finalize", "package")  # PR opened (or skipped/failed) → build the zip output
    graph.add_edge("package", END)         # zip ready (or failed) → done

    # Checkpointer kept only so get_state(config) can read a finished run; there are no interrupts.
    return graph.compile(checkpointer=MemorySaver())


# Compiled once at import; FastAPI invokes this.
workflow = build_graph()
