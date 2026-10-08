"""Patching a plan a running graph is still walking.

The invariant with teeth is that ``work_items[:cursor]`` is immutable. ``select_work_item_node`` sets
``current_work_item = items[index]`` and then advances ``work_item_index`` to ``index + 1``, so every
index below the cursor is already generated, gated and committed — and pushed, when push is on.
Rewriting one would regenerate published work; renumbering the tail would skip an item outright. Most
of these tests exist to pin that boundary rather than the happy path.

The second theme is that nothing raises. A message typed mid-run must not be able to kill a
multi-hour build, so unknown ids, malformed items and unlocatable requests all come back as report
lines. A refusal is a success.
"""

from __future__ import annotations

import pytest

from app.models import WorkItem
from app.services import requirement_ledger as rl
from app.services.plan_patch import PatchOp, PlanPatch, apply_patch, plan_diff


def _plan() -> list[WorkItem]:
    return [
        WorkItem(id="WI-001", feature_id="F-1", feature_title="Login", requirement_ids=["REQ-1"],
                 target_files=["backend/login.py"]),
        WorkItem(id="WI-002", feature_id="F-1", feature_title="Login", requirement_ids=["REQ-1"],
                 target_files=["frontend/Login.tsx"]),
        WorkItem(id="WI-003", feature_id="F-2", feature_title="Wishlist", requirement_ids=["REQ-7"],
                 target_files=["backend/wishlist.py"]),
        WorkItem(id="WI-004", feature_id="F-2", feature_title="Wishlist", requirement_ids=["REQ-7"],
                 target_files=["frontend/Wishlist.tsx"]),
    ]


def _ledger_through(n: int) -> list[dict]:
    """A ledger as it would look after the first ``n`` items generated."""
    led: list[dict] = []
    for it in _plan()[:n]:
        led = rl.record(led, it, list(it.target_files))
    return led


# --------------------------------------------------------------- the cursor boundary

@pytest.mark.parametrize("op", ["modify", "remove"])
def test_items_before_the_cursor_are_never_mutated(op):
    before = _plan()
    cursor = 2                      # WI-001, WI-002 generated and published
    patch = PlanPatch(ops=[PatchOp(op=op, target_work_item_ids=["WI-001"], intent="use OTP")])

    after, report = apply_patch(before, cursor, patch, _ledger_through(2))

    assert after[:cursor] == before[:cursor], "published items must be byte-identical"
    assert [i.id for i in after[:cursor]] == ["WI-001", "WI-002"]
    assert report["removed"] == [], "a generated item is never dropped from the plan"


def test_apply_patch_never_moves_the_cursor():
    # apply_patch has no way to write the cursor; this pins that it also does not need to, by
    # asserting the pending tail still begins at the same item afterwards.
    items = _plan()
    cursor = 2
    patch = PlanPatch(ops=[PatchOp(op="remove", target_work_item_ids=["WI-003"])])
    after, _ = apply_patch(items, cursor, patch, _ledger_through(2))
    assert after[cursor].id == "WI-004", "removing WI-003 must not shift a published item into the cursor slot"


def test_removing_a_pending_item_does_not_disturb_the_published_prefix():
    items = _plan()
    after, report = apply_patch(
        items, 2, PlanPatch(ops=[PatchOp(op="remove", target_work_item_ids=["WI-003"])]), _ledger_through(2)
    )
    assert [i.id for i in after] == ["WI-001", "WI-002", "WI-004"]
    assert report["removed"] == ["WI-003"]


# --------------------------------------------------------------- add

def test_add_appends_a_valid_item():
    after, report = apply_patch(
        _plan(), 2,
        PlanPatch(ops=[PatchOp(op="add", new_items=[{
            "id": "WI-100", "feature_title": "Logout", "target_files": ["backend/logout.py"],
        }])]),
        _ledger_through(2),
    )
    assert [i.id for i in after][-1] == "WI-100"
    assert report["added"] == ["WI-100"]
    assert report["errors"] == []


def test_add_with_an_invalid_item_is_reported_not_raised():
    after, report = apply_patch(
        _plan(), 0,
        PlanPatch(ops=[PatchOp(op="add", new_items=[{"nope": True}, {"id": ""}])]),
        [],
    )
    assert len(after) == 4, "nothing was appended"
    assert len(report["errors"]) == 2
    assert report["added"] == []


def test_add_refuses_a_duplicate_id():
    after, report = apply_patch(
        _plan(), 0, PlanPatch(ops=[PatchOp(op="add", new_items=[{"id": "WI-001"}])]), []
    )
    assert len(after) == 4
    assert any("duplicate" in e for e in report["errors"])


def test_add_with_no_items_is_skipped():
    _, report = apply_patch(_plan(), 0, PlanPatch(ops=[PatchOp(op="add")]), [])
    assert any("no new_items" in s for s in report["skipped"])


# --------------------------------------------------------------- modify

def test_modify_a_pending_item_rewrites_it_in_place_and_keeps_action_create():
    # Nothing is on disk for it yet, so there is nothing to edit — retarget the spec instead.
    after, report = apply_patch(
        _plan(), 2,
        PlanPatch(ops=[PatchOp(op="modify", target_work_item_ids=["WI-004"], intent="use a grid")]),
        _ledger_through(2),
    )
    assert len(after) == 4, "no follow-up item is appended for a pending target"
    target = next(i for i in after if i.id == "WI-004")
    assert target.action == "create"
    assert target.change_intent == "use a grid"
    assert report["modified"] == ["WI-004"]


def test_modify_a_generated_item_appends_a_modify_item_over_its_ledger_files():
    after, report = apply_patch(
        _plan(), 2,
        PlanPatch(ops=[PatchOp(op="modify", target_work_item_ids=["WI-001"], intent="use OTP")]),
        _ledger_through(2),
    )
    assert len(after) == 5
    follow = after[-1]
    assert follow.id == "WI-001-m1"
    assert follow.action == "modify"
    assert follow.target_files == ["backend/login.py"], "targets come from the ledger, not the pack"
    assert follow.change_intent == "use OTP"
    assert follow.requirement_ids == ["REQ-1"], "traceability is carried over"
    assert report["applied"]


def test_modifying_the_same_item_twice_yields_distinct_deterministic_ids():
    items, _ = apply_patch(
        _plan(), 2, PlanPatch(ops=[PatchOp(op="modify", target_work_item_ids=["WI-001"], intent="a")]),
        _ledger_through(2),
    )
    items, _ = apply_patch(
        items, 2, PlanPatch(ops=[PatchOp(op="modify", target_work_item_ids=["WI-001"], intent="b")]),
        _ledger_through(2),
    )
    ids = [i.id for i in items]
    assert ids[-2:] == ["WI-001-m1", "WI-001-m2"]
    assert len(set(ids)) == len(ids)


def test_modify_a_generated_item_with_no_ledger_files_is_skipped():
    # Below the cursor but absent from the ledger: there is genuinely nothing to edit.
    _, report = apply_patch(
        _plan(), 2, PlanPatch(ops=[PatchOp(op="modify", target_work_item_ids=["WI-001"], intent="x")]), []
    )
    assert any("records no files" in s for s in report["skipped"])


# --------------------------------------------------------------- remove

def test_removing_a_generated_item_unwires_instead_of_deleting():
    after, report = apply_patch(
        _plan(), 3,
        PlanPatch(ops=[PatchOp(op="remove", target_work_item_ids=["WI-003"], intent="drop wishlist")]),
        _ledger_through(3),
    )
    assert [i.id for i in after][:4] == ["WI-001", "WI-002", "WI-003", "WI-004"], "no file, no history rewritten"
    unwire = after[-1]
    assert unwire.action == "modify"
    assert unwire.target_files == ["backend/wishlist.py"]
    assert "Do NOT delete the files" in unwire.change_intent
    assert report["orphans"] == ["backend/wishlist.py"]


def test_remove_by_requirement_id_resolves_through_the_ledger():
    after, report = apply_patch(
        _plan(), 2,
        PlanPatch(ops=[PatchOp(op="remove", requirement_ids=["REQ-7"], intent="drop wishlist")]),
        _ledger_through(2),
    )
    # REQ-7 is WI-003 + WI-004, both pending at cursor 2 -> both dropped.
    assert [i.id for i in after] == ["WI-001", "WI-002"]
    assert sorted(report["removed"]) == ["WI-003", "WI-004"]


# --------------------------------------------------------------- refusal + robustness

def test_a_refusal_applies_nothing():
    before = _plan()
    after, report = apply_patch(
        before, 0,
        PlanPatch(refusal="too vague to locate", ops=[PatchOp(op="remove", target_work_item_ids=["WI-001"])]),
        [],
    )
    assert after == before
    assert report["refusal"] == "too vague to locate"
    assert report["applied"] == []


def test_unknown_ids_are_skipped_and_never_raise():
    before = _plan()
    after, report = apply_patch(
        before, 0,
        PlanPatch(ops=[
            PatchOp(op="modify", target_work_item_ids=["WI-999"], intent="x"),
            PatchOp(op="remove", target_work_item_ids=["nope"]),
        ]),
        [],
    )
    assert after == before
    assert len(report["skipped"]) == 2
    assert report["errors"] == []


def test_an_unlocatable_request_is_skipped_with_a_reason():
    _, report = apply_patch(
        _plan(), 0, PlanPatch(ops=[PatchOp(op="modify", requirement_ids=["REQ-404"], intent="x")]), []
    )
    assert any("could not locate" in s for s in report["skipped"])


def test_an_empty_patch_is_a_no_op():
    before = _plan()
    after, report = apply_patch(before, 2, PlanPatch(), _ledger_through(2))
    assert after == before
    assert report["applied"] == []


def test_a_cursor_beyond_the_plan_is_clamped():
    before = _plan()
    after, report = apply_patch(
        before, 999, PlanPatch(ops=[PatchOp(op="remove", target_work_item_ids=["WI-004"])]), []
    )
    assert after == before, "everything is behind a clamped cursor, so nothing is pending"
    assert report["skipped"]


def test_report_is_plain_builtins_for_the_checkpointer():
    _, report = apply_patch(
        _plan(), 2, PlanPatch(ops=[PatchOp(op="modify", target_work_item_ids=["WI-001"], intent="x")]),
        _ledger_through(2),
    )
    assert all(isinstance(v, (list, str)) for v in report.values())
    assert all(isinstance(x, str) for v in report.values() if isinstance(v, list) for x in v)


def test_plan_diff_summarizes_the_turn():
    before = _plan()
    after, _ = apply_patch(
        before, 2, PlanPatch(ops=[PatchOp(op="remove", target_work_item_ids=["WI-003"])]), _ledger_through(2)
    )
    d = plan_diff(before, after)
    assert d == {"added": [], "removed": ["WI-003"], "count_before": 4, "count_after": 3}
