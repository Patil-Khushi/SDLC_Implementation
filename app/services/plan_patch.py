"""Apply a conversational change to a plan a running graph is still walking — deterministic, no LLM.

This is the most safety-critical module in conversational code generation, because it mutates
``WorkflowState["work_items"]`` while the loop is iterating it. The loop is what makes that possible
at all: ``select_work_item_node`` re-reads both ``work_items`` and ``work_item_index`` on *every*
pass, so a plan edited between items is simply picked up — no ``interrupt()``, no ``update_state()``,
no pause. It is also what makes an off-by-one here able to skip or re-run a work item.

**The one invariant everything rests on: only ``work_items[cursor:]`` may be touched.**
``select_work_item_node`` sets ``current_work_item = items[index]`` and then advances
``work_item_index`` to ``index + 1``, so by the time intake runs, every index below the cursor has
already been generated, gated, committed by ``feature_publish`` and — when push is on — pushed to
GitHub. Rewriting one of those would silently regenerate published work or, worse, renumber the tail
so an item is skipped entirely. Indices below the cursor are therefore immutable, and the cursor
itself is never moved by this module.

The division of labour with the LLM is deliberate. The planner
(``app/agents/chat_planner.py``) proposes only *what* changed — ops, work-item / requirement ids,
and intent prose. It never names a file path. Path resolution happens here, in code, from the
requirement ledger's record of what was actually written. A model that could name paths directly
could name any path in the project, and the plan would no longer be reviewable.

Nothing in here raises. A conversational turn arriving mid-run must never be able to kill a
multi-hour build, so every problem — an unknown id, a malformed item, a target already generated,
a request that cannot be located — comes back as a line in the report. A refusal is likewise a
successful outcome, not an error: declining a request the planner cannot safely locate is the
correct behaviour, and the report says so.

Deletion is deliberately soft. ``remove`` never deletes a file and never rewrites history, because
there is no ``Executor.delete_file`` and because per-feature commits may already be pushed. Instead
it drops not-yet-generated items from the tail, and for generated ones appends an ``action="modify"``
item whose intent is to unwire the functionality, listing the now-dead files under ``orphans`` for the
report.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.models import WorkItem
from app.services import requirement_ledger

logger = logging.getLogger(__name__)


class PatchOp(BaseModel):
    """One requested change to the plan.

    ``target_work_item_ids`` is preferred over ``requirement_ids``: requirement attribution from the
    design pack is unreliable today (``design_pack._build_tag_reqs`` matches a singularised OpenAPI
    tag as a substring of a feature name, so one tag can absorb a whole feature's requirements while
    a sibling matches none). Ids resolved through the ledger do not have that problem, so requirement
    lookup is a fallback, not the primary path.
    """

    op: Literal["add", "modify", "remove"]
    target_work_item_ids: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    intent: str = ""
    new_items: list[dict[str, Any]] = Field(default_factory=list)


class PlanPatch(BaseModel):
    """The planner's structured proposal, or its refusal.

    A non-empty ``refusal`` means the planner declined — too vague to locate, a rename or signature
    change it cannot do safely without a call graph, or cross-cutting. That is a success.
    """

    ops: list[PatchOp] = Field(default_factory=list)
    notes: str = ""
    refusal: str = ""


def _empty_report() -> dict[str, Any]:
    return {
        "applied": [], "skipped": [], "errors": [], "orphans": [],
        "added": [], "removed": [], "modified": [], "refusal": "", "notes": "",
    }


def _derive_id(base: str, taken: set[str]) -> str:
    """A deterministic, collision-free id for a follow-up modify item: ``WI-007-m1``, ``-m2``, ...

    Deterministic so a replayed or resumed run produces the same plan, and suffixed from the source
    item so the lineage of a conversational edit stays readable in commits and reports.
    """
    n = 1
    while f"{base}-m{n}" in taken:
        n += 1
    return f"{base}-m{n}"


def _resolve_targets(op: PatchOp, items: list[WorkItem], ledger: list[dict[str, Any]]) -> list[str]:
    """Work-item ids this op refers to, by explicit id first and requirement ids as a fallback."""
    if op.target_work_item_ids:
        return [t for t in op.target_work_item_ids if t]
    resolved = requirement_ledger.work_item_ids_for_requirements(ledger, op.requirement_ids)
    if resolved:
        return resolved
    # Nothing generated yet for those requirements — fall back to the pending plan's own linkage.
    wanted = {r for r in (op.requirement_ids or []) if r}
    return [it.id for it in items if wanted & set(it.requirement_ids or [])] if wanted else []


def apply_patch(
    work_items: list[WorkItem],
    cursor: int,
    patch: PlanPatch,
    ledger: list[dict[str, Any]] | None,
) -> tuple[list[WorkItem], dict[str, Any]]:
    """Apply ``patch`` to the pending tail of ``work_items``; return ``(new_items, report)``.

    ``cursor`` is ``WorkflowState["work_item_index"]`` — the index of the NEXT item to be selected.
    Never raises; never moves the cursor; never touches ``work_items[:cursor]``.
    """
    report = _empty_report()
    report["notes"] = patch.notes or ""
    items = list(work_items or [])
    ledger = ledger or []
    cursor = max(0, min(int(cursor or 0), len(items)))

    if patch.refusal:
        report["refusal"] = patch.refusal
        report["skipped"].append(f"planner refused: {patch.refusal}")
        return items, report

    index_of = {it.id: i for i, it in enumerate(items)}
    taken = set(index_of)

    for op in patch.ops or []:
        if op.op == "add":
            for raw in op.new_items or []:
                try:
                    new_item = WorkItem(**raw)
                except (ValidationError, TypeError) as exc:
                    report["errors"].append(f"add: invalid work item {raw!r}: {exc}")
                    continue
                if new_item.id in taken:
                    report["errors"].append(f"add: duplicate work item id {new_item.id!r}")
                    continue
                items.append(new_item)
                taken.add(new_item.id)
                index_of[new_item.id] = len(items) - 1
                report["added"].append(new_item.id)
                report["applied"].append(f"add {new_item.id}")
            if not op.new_items:
                report["skipped"].append("add: no new_items supplied")
            continue

        targets = _resolve_targets(op, items, ledger)
        if not targets:
            report["skipped"].append(
                f"{op.op}: could not locate a work item from ids={op.target_work_item_ids} "
                f"reqs={op.requirement_ids}"
            )
            continue

        for target in targets:
            idx = index_of.get(target)
            if idx is None:
                report["skipped"].append(f"{op.op}: unknown work item {target!r}")
                continue

            generated = requirement_ledger.is_generated(ledger, target)
            pending = idx >= cursor and not generated

            if op.op == "modify":
                if pending:
                    # Not written yet: retarget it in place. It stays action="create" because there
                    # is still nothing on disk to edit.
                    src = items[idx]
                    items[idx] = src.model_copy(update={
                        "change_intent": op.intent or src.change_intent,
                    })
                    report["modified"].append(target)
                    report["applied"].append(f"modify {target} in place (not yet generated)")
                    continue

                files = requirement_ledger.files_for_work_item(ledger, target)
                if not files:
                    report["skipped"].append(
                        f"modify: {target} is already past the cursor but the ledger records no "
                        "files for it, so there is nothing to edit"
                    )
                    continue
                new_id = _derive_id(target, taken)
                follow_up = WorkItem(
                    id=new_id,
                    feature_id=items[idx].feature_id,
                    feature_title=items[idx].feature_title,
                    requirement_ids=list(items[idx].requirement_ids or []),
                    target_files=files,
                    action="modify",
                    change_intent=op.intent,
                )
                items.append(follow_up)
                taken.add(new_id)
                index_of[new_id] = len(items) - 1
                report["modified"].append(new_id)
                report["applied"].append(f"modify {target} via new item {new_id} over {len(files)} file(s)")
                continue

            # op == "remove"
            if pending:
                removed = items.pop(idx)
                index_of = {it.id: i for i, it in enumerate(items)}
                report["removed"].append(removed.id)
                report["applied"].append(f"remove {removed.id} from the pending plan (never generated)")
                continue

            files = requirement_ledger.files_for_work_item(ledger, target)
            if not files:
                report["skipped"].append(
                    f"remove: {target} is already generated but the ledger records no files for it"
                )
                continue
            new_id = _derive_id(target, taken)
            unwire = WorkItem(
                id=new_id,
                feature_id=items[idx].feature_id,
                feature_title=items[idx].feature_title,
                requirement_ids=list(items[idx].requirement_ids or []),
                target_files=files,
                action="modify",
                change_intent=(
                    f"Remove this functionality. {op.intent}\n\n"
                    "Unwire it from the rest of the project: drop its routes/mounts, exports, "
                    "imports and any now-dead references. Do NOT delete the files themselves; "
                    "leave each one importable and syntactically valid even if empty of behaviour."
                ).strip(),
            )
            items.append(unwire)
            taken.add(new_id)
            index_of[new_id] = len(items) - 1
            report["orphans"].extend(files)
            report["modified"].append(new_id)
            report["applied"].append(f"remove {target} by unwiring via {new_id} ({len(files)} file(s) orphaned)")

    report["orphans"] = sorted(set(report["orphans"]))
    logger.info(
        "plan patch: +%d -%d ~%d (%d skipped, %d errors)",
        len(report["added"]), len(report["removed"]), len(report["modified"]),
        len(report["skipped"]), len(report["errors"]),
    )
    return items, report


def plan_diff(before: list[WorkItem], after: list[WorkItem]) -> dict[str, Any]:
    """Human-readable before/after of a patch — the payload for the SSE event and the harness."""
    b = {it.id for it in before or []}
    a = {it.id for it in after or []}
    return {
        "added": sorted(a - b),
        "removed": sorted(b - a),
        "count_before": len(before or []),
        "count_after": len(after or []),
    }
