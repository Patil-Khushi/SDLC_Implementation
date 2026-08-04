"""``app.graph.graph.resolve_checkpoint_db_path`` — regression coverage for a review-flagged
papercut: ``Settings.checkpoint_db_path`` defaults to a RELATIVE string
(``"app/workspace/checkpoints.sqlite"``), and ``sqlite3.connect`` resolves a relative path against
the process's current working directory at connect time. Two launches from different CWDs (the
FastAPI app from the repo root, a demo script from ``scripts/``, an IDE run config with its own
CWD, ...) would otherwise silently connect to two DIFFERENT files, splitting one project's
checkpoint history in two — defeating the crash-resume feature the checkpointer exists for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.graph.graph import _SERVICE_ROOT, resolve_checkpoint_db_path


def test_memory_sentinel_passes_through_unchanged() -> None:
    assert resolve_checkpoint_db_path(":memory:") == ":memory:"


def test_relative_path_resolves_under_the_service_root_not_cwd() -> None:
    resolved = resolve_checkpoint_db_path("app/workspace/checkpoints.sqlite")
    assert Path(resolved).is_absolute()
    assert Path(resolved) == _SERVICE_ROOT / "app" / "workspace" / "checkpoints.sqlite"


def test_resolution_is_invariant_to_the_process_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exact bug: the SAME configured relative path must resolve to the SAME absolute file
    regardless of which directory the process happens to be launched from."""
    from_repo_root = resolve_checkpoint_db_path("app/workspace/checkpoints.sqlite")

    monkeypatch.chdir(tmp_path)   # simulate launching from some unrelated directory
    from_elsewhere = resolve_checkpoint_db_path("app/workspace/checkpoints.sqlite")

    assert from_repo_root == from_elsewhere


def test_already_absolute_path_is_left_alone(tmp_path: Path) -> None:
    abs_path = str(tmp_path / "custom-checkpoints.sqlite")
    assert resolve_checkpoint_db_path(abs_path) == abs_path


def test_no_argument_reads_the_configured_setting() -> None:
    # Exercises the get_settings() default branch (db_path=None) rather than only the explicit-arg
    # form the other tests use.
    resolved = resolve_checkpoint_db_path()
    assert resolved == ":memory:" or Path(resolved).is_absolute()
