"""LangGraph workflow definition — the IMP-001 code-generation subgraph.

Renders the boilerplate scaffold once, then loops over the plan's work items: generate → fixed
gate (files_complete ONLY — did it write every target file? no compile/build) → back to select
(no per-item commit) | repair→gate | escalate (failure). Once the plan is exhausted, the run
auto-commits (one run-level commit), then: Code Review clones the (possibly just-pushed) repo and
writes its report/``findings.json`` → the post-commit Debugging<->Unit-Test loop verifies the
project still compiles/builds/passes (with an LLM debugging repair path on failure) →
Documentation writes a README from the final source → Security clones the repo again, runs
Semgrep, and writes a verdict (``approve`` | ``changes_requested``) into its report — advisory
only, nothing downstream currently acts on the verdict. NO human approval step anywhere. The
fixed gate/check nodes are the router source; the local repair/debug caps live in router.py.

    scaffold → select → code_generator → gate ─┬─ pass ──────────────→ select (loop)
                  ▲                             ├─ fail, repair<CAP ─→ repair → gate
                  │                             └─ fail, repair>=CAP → escalate → END
                  │                                                    (needs_human_review)
                  └── select: nothing left → commit
                                                  │
                                                  ▼
                                             code_review
                                                  │
                                                  ▼
                                             debug_check ─┬─ pass, no tests yet ─→ unit_test_generate
                                                           ├─ pass, tests exist ──→ unit_test_run
                                                           ├─ fail, debug<CAP ────→ debugging → debug_check
                                                           └─ fail, debug>=CAP ───→ escalate → END
                                             unit_test_generate ─┬─ ok ──→ unit_test_run
                                                                 └─ fail → escalate → END
                                             unit_test_run ─┬─ pass ──→ documentation → security → END (done)
                                                             ├─ fail, debug<CAP ─→ debugging → debug_check
                                                             └─ fail, debug>=CAP → escalate → END

Code Review/Documentation/Security each run ONCE, only on this clean completion path — every
escalate branch above bypasses all of them entirely, same as it bypasses the debug/test loop.
Documentation and Security are straight-line fixed edges — neither fails the run on a bad LLM
reply or a missing ``repo_url``; each degrades gracefully (an empty report/no-op) instead, so
neither needs its own cap or escalate branch.

(Merging a Security-approved `dev` into `main`, and any automated fix-it loop for a
`changes_requested` verdict, are explicitly NOT wired here — `main` already has its own Code
Review-driven Refactoring stage in this same region of the graph; reconciling the two is a
follow-up integration decision, not part of this change.)

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
from app.agents.repair import repair_node
from app.graph import nodes
from app.graph.router import (
    route_after_codegen,
    route_after_debug_check,
    route_after_gate,
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
    graph.add_node("commit", nodes.commit_node)
    graph.add_node("repair", repair_node)
    graph.add_node("escalate", nodes.escalate_node)
    graph.add_node("code_review", nodes.code_review_node)
    graph.add_node("debug_check", nodes.debug_check_node)
    graph.add_node("debugging", debugging_node)
    graph.add_node("unit_test_generate", nodes.unit_test_generate_node)
    graph.add_node("unit_test_run", nodes.unit_test_run_node)
    graph.add_node("documentation", nodes.documentation_node)
    graph.add_node("security", nodes.security_node)

    graph.add_edge(START, "scaffold")
    graph.add_edge("scaffold", "select")
    graph.add_conditional_edges(
        "select",
        route_after_select,
        {"code_generator": "code_generator", "commit": "commit"},
    )
    graph.add_conditional_edges(
        "code_generator", route_after_codegen, {"gate": "gate", "escalate": "escalate"}
    )
    graph.add_conditional_edges(
        "gate", route_after_gate, {"select": "select", "repair": "repair", "escalate": "escalate"}
    )
    graph.add_edge("commit", "code_review")       # single run-level commit → Code Review
    graph.add_edge("code_review", "debug_check")  # TODO: code_review -> refactoring -> refactor_commit
                                                   # -> debug_check once Refactoring's branch lands
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
    graph.add_conditional_edges(
        "unit_test_run",
        route_after_test_run,
        {"done": "documentation", "debugging": "debugging", "escalate": "escalate"},
    )
    graph.add_edge("debugging", "debug_check")  # debugging → back to the fixed debug/build check
    graph.add_edge("documentation", "security")
    graph.add_edge("security", END)            # scan run, report written → done (auto, no approval)

    # Checkpointer kept only so get_state(config) can read a finished run; there are no interrupts.
    return graph.compile(checkpointer=MemorySaver())


# Compiled once at import; FastAPI invokes this.
workflow = build_graph()
