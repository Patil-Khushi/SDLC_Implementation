"""LangGraph workflow definition — the IMP-001 code-generation subgraph.

Renders the boilerplate scaffold once, then loops over the plan's work items: generate → fixed
gate (files_complete ONLY — did it write every target file? no compile/build) → back to select
(no per-item commit) | repair→gate | escalate→HITL. Once the plan (and any rework queue) is
exhausted, one batch_review interrupt decides the whole run: approved → a single run-level
commit; rejected → the named items are drained back through repair. The fixed gate is the
router; the local repair cap lives in router.py.

    scaffold → select → code_generator → gate ─┬─ pass ──────────────→ select (loop)
                  ▲    ╲                        ├─ fail, repair<CAP ─→ repair → gate
                  │      ╲(rework item)          └─ fail, repair>=CAP → escalate → human_review
                  │        ╲                                                       (interrupt, ends the run)
                  │          ▼
                  │        repair → gate (same as above)
                  │
                  └── select: nothing left → batch_review → batch_review_wait (interrupt)
                                                                 ├─ approved → commit → done
                                                                 └─ rejected → review_feedback → select (loop)

``batch_review`` persists ``workflow_status`` and returns BEFORE the interrupt in
``batch_review_wait`` — a node that mutates state and then calls interrupt() in the same
function loses that mutation on the pausing pass (mirrors the existing escalate/human_review
split).

Compiled with a checkpointer so the batch_review_wait / human_review interrupt()s can pause the
run for HITL.
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agents.repair import repair_node
from app.graph import nodes
from app.graph.router import (
    route_after_batch_review,
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
    graph.add_node("human_review", nodes.human_review_node)
    graph.add_node("batch_review", nodes.batch_review_node)
    graph.add_node("batch_review_wait", nodes.batch_review_wait_node)

    graph.add_edge(START, "scaffold")
    graph.add_edge("scaffold", "select")
    graph.add_conditional_edges(
        "select",
        route_after_select,
        {"code_generator": "code_generator", "repair": "repair", "batch_review": "batch_review"},
    )
    graph.add_conditional_edges(
        "code_generator", route_after_codegen, {"gate": "gate", "escalate": "escalate"}
    )
    graph.add_conditional_edges(
        "gate", route_after_gate, {"select": "select", "repair": "repair", "escalate": "escalate"}
    )
    graph.add_edge("batch_review", "batch_review_wait")  # persist status, THEN interrupt
    graph.add_conditional_edges(
        "batch_review_wait", route_after_batch_review, {"commit": "commit", "select": "select"}
    )
    graph.add_edge("commit", END)            # single run-level commit → done
    graph.add_edge("repair", "gate")          # repair → back to the fixed gate
    graph.add_edge("escalate", "human_review")
    graph.add_edge("human_review", END)

    # Checkpointer enables the batch_review_wait / human_review interrupt()s to pause/resume (HITL).
    return graph.compile(checkpointer=MemorySaver())


# Compiled once at import; FastAPI invokes this.
workflow = build_graph()
