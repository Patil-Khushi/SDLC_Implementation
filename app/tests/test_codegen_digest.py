"""Proving a *modify* work item actually changed something.

``files_complete`` — the only code-generation gate — asks whether the target exists. For a create
item that is the right question. For a modify item the file already exists, so the gate passes even
when the agent wrote nothing, and the run then commits and reports a change that never happened.
These tests pin the check that closes that hole.

Pure executor reads: no filesystem, no git, no LLM.
"""

from __future__ import annotations

from app.integrations.executor import FakeExecutor
from app.services.codegen_digest import (
    any_target_changed,
    changed_paths,
    digest_of,
    snapshot,
    unchanged_paths,
)

_PROJECT = "myapp"


def _executor(files: dict[str, str]) -> FakeExecutor:
    # FakeExecutor keys are full workspace paths, which is what project_path() builds.
    return FakeExecutor(files={f"{_PROJECT}/{rel}": body for rel, body in files.items()})


def test_digest_is_stable_and_content_sensitive():
    assert digest_of("hello") == digest_of("hello")
    assert digest_of("hello") != digest_of("hello ")
    assert len(digest_of("")) == 64


def test_snapshot_keys_are_project_relative():
    # Keys must stay project-relative so a snapshot survives a checkpoint round-trip and is
    # comparable across executors.
    ex = _executor({"backend/login.py": "def login(): ...\n"})
    snap = snapshot(ex, _PROJECT, ["backend/login.py"])
    assert list(snap) == ["backend/login.py"]
    assert snap["backend/login.py"] == digest_of("def login(): ...\n")


def test_an_unreadable_path_is_absent_rather_than_raising():
    # A target that does not exist yet is a legitimate state, not an error.
    ex = _executor({"backend/login.py": "x\n"})
    snap = snapshot(ex, _PROJECT, ["backend/login.py", "backend/missing.py"])
    assert set(snap) == {"backend/login.py"}


def test_snapshot_tolerates_empty_and_blank_input():
    ex = _executor({"a.py": "x"})
    assert snapshot(ex, _PROJECT, []) == {}
    assert snapshot(ex, _PROJECT, None) == {}
    assert snapshot(ex, _PROJECT, ["", "a.py"]) == {"a.py": digest_of("x")}


def test_changed_and_unchanged_partition_the_after_snapshot():
    before = {"a.py": digest_of("1"), "b.py": digest_of("2")}
    after = {"a.py": digest_of("1-edited"), "b.py": digest_of("2")}
    assert changed_paths(before, after) == ["a.py"]
    assert unchanged_paths(before, after) == ["b.py"]


def test_a_file_created_during_the_item_counts_as_changed():
    # Absent in `before`, present in `after` — the tree really did move.
    before = {"a.py": digest_of("1")}
    after = {"a.py": digest_of("1"), "new.py": digest_of("fresh")}
    assert changed_paths(before, after) == ["new.py"]
    assert unchanged_paths(before, after) == ["a.py"]


def test_any_target_changed_is_the_gates_question():
    same = {"a.py": digest_of("1")}
    assert any_target_changed(same, same) is False
    assert any_target_changed(same, {"a.py": digest_of("2")}) is True


def test_a_noop_edit_is_detected_end_to_end():
    # The exact failure the module exists to catch: the agent "modified" the file without changing it.
    ex = _executor({"backend/login.py": "def login(): ...\n"})
    before = snapshot(ex, _PROJECT, ["backend/login.py"])
    after = snapshot(ex, _PROJECT, ["backend/login.py"])
    assert any_target_changed(before, after) is False

    ex.write_file(f"{_PROJECT}/backend/login.py", "def login(otp): ...\n")
    after = snapshot(ex, _PROJECT, ["backend/login.py"])
    assert any_target_changed(before, after) is True
    assert changed_paths(before, after) == ["backend/login.py"]


def test_comparisons_tolerate_none():
    assert changed_paths(None, None) == []
    assert unchanged_paths(None, None) == []
    assert any_target_changed(None, None) is False
