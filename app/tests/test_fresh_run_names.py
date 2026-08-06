"""``--fresh``: never write into, or push onto, a previous run's project.

A project name occupies three homes at once — the local product folder, the GitHub repo, and the
LangGraph checkpoint — and reusing any of them breaks the next run differently: the scaffold writes
into last run's working tree, features push onto last run's branches, and stale state fields
silently skip whole phases. ``main`` already aborts on the checkpoint case and tells the operator to
pick another name; ``--fresh`` automates exactly that choice (and widens it to the other two), for
commands meant to be repeatable — a nightly build, a demo recording.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from scripts import run_fixture  # noqa: E402


@pytest.fixture(autouse=True)
def _no_checkpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """No LangGraph checkpoints unless a test says otherwise (real ones would hit the run's DB)."""
    monkeypatch.setattr(
        run_fixture.workflow, "get_state", lambda _cfg: SimpleNamespace(values={})
    )


def _no_github(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Stub `gh`: nothing exists remotely. Returns the commands attempted, for assertions."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: Any) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(run_fixture.subprocess, "run", fake_run)
    return calls


def test_unused_name_is_kept_exactly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_github(monkeypatch)
    name = run_fixture._fresh_project_name(
        "vp-demo", out_base=tmp_path, owner="o", check_github=False
    )
    assert name == "vp-demo"  # no gratuitous suffix — the predictable name survives


def test_existing_local_folder_forces_a_new_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_github(monkeypatch)
    (tmp_path / "vp-demo").mkdir()

    name = run_fixture._fresh_project_name(
        "vp-demo", out_base=tmp_path, owner="o", check_github=False
    )

    assert name != "vp-demo"
    assert name.startswith("vp-demo-")
    assert not (tmp_path / name).exists()  # ...and the replacement is genuinely unused


def test_existing_checkpoint_forces_a_new_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The case main's guard aborts on: no folder, no repo, but a checkpoint whose stale fields
    # would make the graph skip phases.
    _no_github(monkeypatch)
    monkeypatch.setattr(
        run_fixture.workflow, "get_state",
        lambda cfg: SimpleNamespace(
            values={"workflow_status": "completed"} if cfg["configurable"]["thread_id"] == "vp-demo" else {}
        ),
    )

    name = run_fixture._fresh_project_name(
        "vp-demo", out_base=tmp_path, owner="o", check_github=False
    )
    assert name.startswith("vp-demo-") and name != "vp-demo"


def test_existing_github_repo_forces_a_new_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing local, but the repo exists remotely — features would push onto its branches.
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: Any) -> SimpleNamespace:
        seen.append(cmd)
        exists = cmd[:2] == ["gh", "repo"] and cmd[-1] == "o/vp-demo"
        return SimpleNamespace(returncode=0 if exists else 1, stdout="", stderr="")

    monkeypatch.setattr(run_fixture.subprocess, "run", fake_run)

    name = run_fixture._fresh_project_name(
        "vp-demo", out_base=tmp_path, owner="o", check_github=True
    )

    assert name.startswith("vp-demo-") and name != "vp-demo"
    assert any(cmd[:2] == ["gh", "repo"] for cmd in seen)


def test_github_is_not_consulted_when_not_publishing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # --no-publish creates no repo, so a remote check is a pointless network round-trip.
    calls = _no_github(monkeypatch)
    run_fixture._fresh_project_name("vp-demo", out_base=tmp_path, owner="o", check_github=False)
    assert not any(cmd[:2] == ["gh", "repo"] for cmd in calls)


def test_unreachable_github_does_not_invent_a_collision(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    # `gh` missing or offline must not silently rename every run (and thus orphan the repo the
    # operator expected); an unanswerable question is not a "yes".
    def boom(*_a: Any, **_kw: Any) -> SimpleNamespace:
        raise OSError("gh not found")

    monkeypatch.setattr(run_fixture.subprocess, "run", boom)
    assert run_fixture._fresh_project_name(
        "vp-demo", out_base=tmp_path, owner="o", check_github=True
    ) == "vp-demo"


def test_suffixed_name_that_is_also_taken_keeps_counting(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    # Two runs started inside the same second must not collide on the timestamp.
    _no_github(monkeypatch)
    (tmp_path / "vp-demo").mkdir()
    taken = {"vp-demo"}

    real_is_taken = run_fixture._name_is_taken
    monkeypatch.setattr(
        run_fixture, "_name_is_taken",
        lambda name, **kw: name in taken or real_is_taken(name, **kw),
    )
    first = run_fixture._fresh_project_name("vp-demo", out_base=tmp_path, owner="o", check_github=False)
    taken.add(first)
    second = run_fixture._fresh_project_name("vp-demo", out_base=tmp_path, owner="o", check_github=False)

    assert second != first and second.startswith("vp-demo-")


def test_owner_resolution_prefers_the_explicit_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_OWNER", "from-env")
    assert run_fixture._resolve_owner("from-flag") == "from-flag"
    assert run_fixture._resolve_owner(None) == "from-env"


def test_owner_falls_back_to_gh_when_nothing_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_OWNER", raising=False)
    monkeypatch.setattr(
        run_fixture.subprocess, "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout="gh-account\n", stderr=""),
    )
    assert run_fixture._resolve_owner(None) == "gh-account"
