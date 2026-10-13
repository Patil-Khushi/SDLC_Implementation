"""The ledger is how an incremental change stays small.

Without it, "change the login flow" can only be answered by regenerating everything, because nothing
records which files came out of which work item. These tests pin the two properties the intake path
depends on: rows reflect what was *written* (not what the pack predicted), and re-running a work item
replaces its row instead of accumulating stale file sets — which happens on every gate/repair retry.
"""

from __future__ import annotations

from app.models import WorkItem
from app.services import requirement_ledger as rl


def _item(item_id: str, **kw) -> WorkItem:
    return WorkItem(id=item_id, **kw)


def test_record_is_pure_and_returns_a_new_list():
    original: list[dict] = []
    out = rl.record(original, _item("WI-001", requirement_ids=["REQ-1"]), ["backend/auth.py"])
    assert original == [], "the input ledger must not be mutated"
    assert [r["work_item_id"] for r in out] == ["WI-001"]
    assert out[0]["files"] == ["backend/auth.py"]
    assert out[0]["requirement_ids"] == ["REQ-1"]


def test_rerecording_the_same_item_replaces_its_row():
    # The repair loop re-runs a work item; two rows would make files_for_work_item report a stale
    # set alongside the current one.
    led = rl.record([], _item("WI-001"), ["a.py", "b.py"])
    led = rl.record(led, _item("WI-001"), ["a.py"])
    assert len(led) == 1
    assert rl.files_for_work_item(led, "WI-001") == ["a.py"]


def test_record_stores_observed_files_even_when_they_differ_from_target_files():
    # The whole reason the ledger exists: a model asked for two files often writes three.
    item = _item("WI-002", target_files=["backend/login.py"])
    led = rl.record([], item, ["backend/login.py", "backend/schemas/login.py"])
    assert rl.files_for_work_item(led, "WI-002") == [
        "backend/login.py",
        "backend/schemas/login.py",
    ]


def test_files_are_deduplicated_order_stably():
    led = rl.record([], _item("WI-003"), ["b.py", "a.py", "b.py", "", "a.py"])
    assert rl.files_for_work_item(led, "WI-003") == ["b.py", "a.py"]


def test_is_generated_distinguishes_run_from_pending():
    led = rl.record([], _item("WI-001"), ["a.py"])
    assert rl.is_generated(led, "WI-001") is True
    assert rl.is_generated(led, "WI-999") is False
    assert rl.is_generated([], "WI-001") is False
    assert rl.is_generated(None, "WI-001") is False


def test_files_for_requirements_unions_across_work_items():
    led = rl.record([], _item("WI-001", requirement_ids=["REQ-1", "REQ-2"]), ["auth.py"])
    led = rl.record(led, _item("WI-002", requirement_ids=["REQ-2"]), ["session.py", "auth.py"])
    led = rl.record(led, _item("WI-003", requirement_ids=["REQ-9"]), ["cart.py"])

    assert rl.files_for_requirements(led, ["REQ-2"]) == ["auth.py", "session.py"]
    assert rl.files_for_requirements(led, ["REQ-1", "REQ-9"]) == ["auth.py", "cart.py"]
    assert rl.files_for_requirements(led, ["REQ-404"]) == []
    assert rl.files_for_requirements(led, []) == []
    assert rl.files_for_requirements(led, None) == []


def test_work_item_ids_for_requirements():
    led = rl.record([], _item("WI-001", requirement_ids=["REQ-1"]), ["a.py"])
    led = rl.record(led, _item("WI-002", requirement_ids=["REQ-1", "REQ-3"]), ["b.py"])
    assert rl.work_item_ids_for_requirements(led, ["REQ-1"]) == ["WI-001", "WI-002"]
    assert rl.work_item_ids_for_requirements(led, ["REQ-3"]) == ["WI-002"]


def test_all_files_is_the_blast_radius_input():
    led = rl.record([], _item("WI-001"), ["a.py", "shared.py"])
    led = rl.record(led, _item("WI-002"), ["b.py", "shared.py"])
    assert rl.all_files(led) == ["a.py", "shared.py", "b.py"]
    assert rl.all_files([]) == []


def test_malformed_rows_never_raise():
    # A ledger restored from an older checkpoint may be missing keys entirely.
    led = [{}, {"work_item_id": "WI-001"}, {"files": ["x.py"]}]
    assert rl.files_for_work_item(led, "WI-001") == []
    assert rl.files_for_requirements(led, ["REQ-1"]) == []
    assert rl.is_generated(led, "WI-001") is True
    assert rl.all_files(led) == ["x.py"]


def test_row_carries_the_action_so_intake_can_tell_create_from_modify():
    led = rl.record([], _item("WI-001", action="modify"), ["a.py"])
    assert led[0]["action"] == "modify"


def test_summarize_is_readable_and_handles_empty():
    assert "nothing generated" in rl.summarize([])
    led = rl.record([], _item("WI-001", requirement_ids=["REQ-1"]), ["a.py"])
    assert "WI-001" in rl.summarize(led)
