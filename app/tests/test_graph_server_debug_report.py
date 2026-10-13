"""The dashboard actually shows the Debugging report and each round's outcome.

graph_server and run_facts looked for ``debug_report_path`` / ``debug-report.md`` while the agent
writes ``debugging_report_path`` / ``debugging-report.md``, so the Debugging Report card never
appeared; and the per-stage log cap dropped the round's closing summary line, because a 16+-turn
tool loop alone exceeds it.
"""

from __future__ import annotations

import json
import logging
import queue

import pytest

from app.services.run_facts import build_facts

gs = pytest.importorskip("scripts.graph_server")


def test_report_definition_uses_the_fields_the_agent_writes() -> None:
    row = next(r for r in gs._REPORT_DEFS if r[0] == "debugging")
    assert row[2:] == ("debugging_report_path", "debugging_report", "debugging-report.md")


def _record(msg: str, **extra) -> logging.LogRecord:
    record = logging.LogRecord("app.agents.debugging", logging.INFO, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_ui_always_records_get_past_the_per_stage_line_cap() -> None:
    sink: queue.Queue = queue.Queue()
    label = ["Debugging", gs._MAX_LOG_LINES_PER_NODE + 5]          # cap already exhausted
    handler = gs._SSELogCapture(sink, label)

    handler.emit(_record("tool_loop turn=17/48 ..."))
    handler.emit(_record("[debugging] round 2 (compile/build): 64 failing (was 128) — progress", ui_always=True))

    events = []
    while not sink.empty():
        events.append(json.loads(sink.get().removeprefix("data: ").strip()))
    assert [e["text"] for e in events] == ["[debugging] round 2 (compile/build): 64 failing (was 128) — progress"]


def test_run_facts_lists_the_debugging_report() -> None:
    facts = build_facts({"debugging_report_path": "reports/proj/debugging-report.md"})
    assert "debugging: reports/proj/debugging-report.md" in facts
