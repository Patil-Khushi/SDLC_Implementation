"""LangGraph workflow definition — the IMP-001 code-generation subgraph.

Renders the boilerplate scaffold once, then loops over the plan's work items: generate → fixed
gate (files_complete ONLY — did it write every target file? no compile/build) → back to select
(no per-item commit) | repair→gate | escalate (failure). Once the plan is exhausted, the run
auto-commits (one run-level commit) and finishes — NO human approval step. The fixed gate is the
router; the local repair cap lives in router.py.

    scaffold → select → code_generator → gate ─┬─ pass ──────────────→ select (loop)
                  ▲                             ├─ fail, repair<CAP ─→ repair → gate
                  │                             └─ fail, repair>=CAP → escalate → END
                  │                                                    (needs_human_review)
                  └── select: nothing left → commit → code_review → END (auto, no approval)

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

from app.agents.repair import repair_node
from app.graph import nodes
from app.graph.router import (
    route_after_codegen,
    route_after_gate,
    route_after_select,
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
    graph.add_edge("commit", "code_review")  # clean completion → final review (clone → analysis → report)
    graph.add_edge("code_review", END)        # review written → done
    graph.add_edge("repair", "gate")          # repair → back to the fixed gate
    graph.add_edge("escalate", END)           # failure flagged (needs_human_review) → done, no pause

    # Checkpointer kept only so get_state(config) can read a finished run; there are no interrupts.
    return graph.compile(checkpointer=MemorySaver())


# Compiled once at import; FastAPI invokes this.
workflow = build_graph()
