"""Every graph node must be known to the frontend bridge.

``scripts/graph_server.py`` translates graph nodes into the frontend's stage visual through
``_NODE_TO_STAGE`` and names them in the log stream through ``_NODE_LABEL``. Neither map is derived
from the graph, so adding a node and forgetting the maps is silent: the run works, and the new step
simply never appears in the UI. That is exactly what happened while adding the brownfield
``acquire`` node, and nothing failed.

These tests close the loop in both directions. They are pure dict comparisons — no server, no
network, no run.

Mapping a node to ``None`` is a legitimate, deliberate choice (a step that should be a log line
rather than a tile), so the map must merely CONTAIN every node; it need not give each one a tile.
"""

from __future__ import annotations

import pytest

import scripts.graph_server as gs
from app.graph.graph import build_graph


def _graph_nodes() -> set[str]:
    """Node names from the compiled graph itself, so this can never drift from reality.

    LangGraph injects its own ``__start__``/``__end__`` sentinels, which are not pipeline steps.
    """
    nodes = set(build_graph().get_graph().nodes)
    return {n for n in nodes if not n.startswith("__")}


def test_every_graph_node_is_in_the_stage_map() -> None:
    missing = _graph_nodes() - set(gs._NODE_TO_STAGE)
    assert not missing, (
        f"nodes missing from graph_server._NODE_TO_STAGE: {sorted(missing)} - "
        "without an entry the step never reaches the frontend. Map it to a stage id that exists in "
        "SDLC_Frontend/src/mocks/stages.ts, or to None to stream it as a log line only."
    )


def test_every_graph_node_has_a_log_label() -> None:
    missing = _graph_nodes() - set(gs._NODE_LABEL)
    assert not missing, (
        f"nodes missing from graph_server._NODE_LABEL: {sorted(missing)} - "
        "the log stream would show the raw node key instead of a readable agent name."
    )


def test_the_maps_do_not_name_nodes_that_no_longer_exist() -> None:
    # The other direction: a renamed or removed node leaves a dead entry behind, and the next reader
    # cannot tell a stale key from a real one.
    stale_stages = set(gs._NODE_TO_STAGE) - _graph_nodes()
    stale_labels = set(gs._NODE_LABEL) - _graph_nodes()
    assert not stale_stages, f"_NODE_TO_STAGE names non-existent nodes: {sorted(stale_stages)}"
    assert not stale_labels, f"_NODE_LABEL names non-existent nodes: {sorted(stale_labels)}"


@pytest.mark.parametrize("node", ["scaffold", "acquire"])
def test_both_entry_lanes_are_mapped(node: str) -> None:
    # The two entry nodes specifically: whichever lane a run takes, the bridge has to recognise it.
    assert node in gs._NODE_TO_STAGE
    assert node in gs._NODE_LABEL
