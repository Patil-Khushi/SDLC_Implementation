"""What was ACTUALLY generated, per work item — deterministic, no LLM, no I/O.

This is the data structure that makes an *incremental* change possible. Without it, "change the
login flow" has no answer better than "regenerate the project", because nothing in the run records
which files came out of which work item.

Two design choices are load-bearing:

**It records observed files, not predicted ones.** ``WorkItem.target_files`` is what the design pack
*asked* for; this ledger stores what the generator *wrote*. Those differ in practice — a model asked
for three files routinely writes four, or names one differently — and an incremental edit aimed at
the predicted set would miss the real code. So rows are written from the generator's own result, at
``code_generator._record_success``.

**It is the honest answer to a question the design pack cannot currently answer.** Requirement
traceability from the pack is unreliable today: ``design_pack._build_tag_reqs`` matches a singularised
OpenAPI tag as a *substring* of a feature name, so on the live pack the tag ``orders`` absorbs all of
its feature's requirements while ``returns`` matches none. Anything derived from ``requirement_ids``
alone inherits that error. Resolving by ``work_item_id`` does not, which is why the intake path
prefers ids and treats requirement lookup as best-effort.

Everything here is a pure function over ``list[dict]`` — plain builtins, because this list is carried
on ``WorkflowState`` and the checkpointer allow-lists ``WorkItem`` alone for msgpack (any other
pydantic type silently degrades to a bare dict after a round-trip).
"""

from __future__ import annotations

import logging
from typing import Any

from app.models import WorkItem

logger = logging.getLogger(__name__)


def _row_of(item: WorkItem, files: list[str]) -> dict[str, Any]:
    """One ledger row, as plain builtins."""
    return {
        "work_item_id": item.id,
        "feature_id": item.feature_id,
        "feature_title": item.feature_title,
        "requirement_ids": list(item.requirement_ids or []),
        "files": _dedup(files),
        "action": item.action,
    }


def _dedup(values: list[str] | None) -> list[str]:
    """De-duplicate while preserving first-seen order (stable output matters for reports/diffs)."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values or []:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def record(ledger: list[dict[str, Any]] | None, item: WorkItem, files: list[str]) -> list[dict[str, Any]]:
    """Return a NEW ledger with ``item``'s row set to ``files``.

    Replaces any existing row for the same ``work_item_id`` rather than appending. That is not
    tidiness — the gate/repair loop can run a work item several times, and a duplicated row would
    make ``files_for_work_item`` report a stale file set alongside the current one.

    Pure: ``ledger`` is never mutated.
    """
    rows = [dict(r) for r in (ledger or []) if r.get("work_item_id") != item.id]
    rows.append(_row_of(item, files))
    return rows


def is_generated(ledger: list[dict[str, Any]] | None, work_item_id: str) -> bool:
    """Has this work item already produced files in this run?

    The question ``plan_patch`` asks to decide whether a "modify" request can be satisfied by
    rewriting a pending item in place, or needs a fresh ``action="modify"`` item over real files.
    """
    return any(r.get("work_item_id") == work_item_id for r in (ledger or []))


def files_for_work_item(ledger: list[dict[str, Any]] | None, work_item_id: str) -> list[str]:
    """Files this work item wrote, or ``[]`` if it has not run."""
    for row in ledger or []:
        if row.get("work_item_id") == work_item_id:
            return _dedup(row.get("files"))
    return []


def files_for_requirements(
    ledger: list[dict[str, Any]] | None, requirement_ids: list[str] | None
) -> list[str]:
    """Union of the files of every work item covering any of ``requirement_ids``.

    Best-effort by construction — see the module docstring on why pack-derived ``requirement_ids``
    are not trustworthy today. Callers should prefer ``files_for_work_item`` when they have an id.
    """
    wanted = {r for r in (requirement_ids or []) if r}
    if not wanted:
        return []
    out: list[str] = []
    for row in ledger or []:
        if wanted & set(row.get("requirement_ids") or []):
            out.extend(row.get("files") or [])
    return _dedup(out)


def work_item_ids_for_requirements(
    ledger: list[dict[str, Any]] | None, requirement_ids: list[str] | None
) -> list[str]:
    """Ids of the work items covering any of ``requirement_ids``, order-stable."""
    wanted = {r for r in (requirement_ids or []) if r}
    if not wanted:
        return []
    return _dedup([
        str(row.get("work_item_id") or "")
        for row in ledger or []
        if wanted & set(row.get("requirement_ids") or [])
    ])


def all_files(ledger: list[dict[str, Any]] | None) -> list[str]:
    """Every file the run has generated so far — the input to a blast-radius query."""
    out: list[str] = []
    for row in ledger or []:
        out.extend(row.get("files") or [])
    return _dedup(out)


def summarize(ledger: list[dict[str, Any]] | None) -> str:
    """One-line-per-item rendering, for a report or a planner prompt."""
    if not ledger:
        return "(nothing generated yet)"
    lines = []
    for row in ledger:
        reqs = ", ".join(row.get("requirement_ids") or []) or "-"
        lines.append(
            f"{row.get('work_item_id')} [{row.get('action', 'create')}] "
            f"reqs={reqs} files={len(row.get('files') or [])}"
        )
    return "\n".join(lines)
