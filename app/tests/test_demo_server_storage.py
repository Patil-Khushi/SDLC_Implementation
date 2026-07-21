"""Storage lifecycle for ``scripts/demo_server.py`` ``--real`` mode.

Pins the policy that makes ``demo_server.py --real`` consistent with ``feature_commit.py``'s CLI:

  - No ``--out-dir``  -> a throwaway ``tempfile.mkdtemp(prefix="sdlc-gen-")`` dir; the run REQUIRES
    a push and the working copy is DELETED after a fully successful push, KEPT on any failure.
  - ``--out-dir``     -> that persistent folder; files are kept and NEVER auto-deleted.

All tests are offline: they exercise the pure helpers (``_resolve_out_dir`` / ``_finalize_run`` /
``_push_config_ok`` / ``_guard_ephemeral_push``) and the ``/api/run`` guard with a stubbed
generator, so no API key, no GitHub, and no network are needed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

import scripts.demo_server as ds
import scripts.feature_commit as fc


# --------------------------------------------------------------- Case 1: default -> temp dir

def test_default_no_out_dir_uses_temp_dir() -> None:
    path, is_temp = ds._resolve_out_dir(None, "real")
    try:
        assert is_temp is True
        assert path.is_dir()
        assert path.name.startswith("sdlc-gen-")
        # It lives in the system temp area, NOT in the persistent generated-apps dir.
        assert path.parent == Path(tempfile.gettempdir()).resolve()
        assert path != fc._DEFAULT_OUT_DIR.resolve()
    finally:
        fc._force_rmtree(path)


def test_dry_run_default_is_unchanged_persistent_dir() -> None:
    # Dry-run never pushes, so it keeps the old persistent default (no temp churn), NOT ephemeral.
    path, is_temp = ds._resolve_out_dir(None, "dry-run")
    assert is_temp is False
    assert path == fc._DEFAULT_OUT_DIR


# ------------------------------------------------------- Case 2: successful push -> temp deleted

def test_finalize_deletes_temp_dir_on_success(tmp_path: Path) -> None:
    proj = tmp_path / "sdlc-gen-abc"
    proj.mkdir()
    (proj / "file.txt").write_text("x", encoding="utf-8")

    removed = ds._finalize_run(proj, is_temp=True, success=True)

    assert removed is True
    assert not proj.exists()


# --------------------------------------------------------- Case 3: failed push -> temp preserved

def test_finalize_keeps_temp_dir_on_failure(tmp_path: Path) -> None:
    proj = tmp_path / "sdlc-gen-def"
    proj.mkdir()
    (proj / "file.txt").write_text("x", encoding="utf-8")

    removed = ds._finalize_run(proj, is_temp=True, success=False)

    assert removed is False
    assert proj.exists()  # preserved for recovery


# ------------------------------------------------ Case 4: no push config -> rejected before gen

def test_guard_rejects_ephemeral_without_push(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "MODE", "real")
    monkeypatch.setattr(ds, "OUT_DIR_IS_TEMP", True)
    monkeypatch.setattr(ds, "_env_token", lambda: "")
    monkeypatch.setattr(ds, "_gh", lambda *a, **k: (1, "", "not logged in"))

    with pytest.raises(HTTPException) as exc:
        ds._guard_ephemeral_push()
    assert exc.value.status_code == 400
    assert "--out-dir" in exc.value.detail


def test_run_endpoint_aborts_before_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "MODE", "real")
    monkeypatch.setattr(ds, "OUT_DIR_IS_TEMP", True)
    monkeypatch.setattr(ds, "_env_token", lambda: "")
    monkeypatch.setattr(ds, "_gh", lambda *a, **k: (1, "", "not logged in"))
    # Skip pack validation (needs fixtures) — we only care that generation is never reached.
    monkeypatch.setattr(ds, "_prepare_feature_run",
                        lambda pack, project, only: (Path("."), [("US-01", "t", "b")], "app"))

    called = {"gen": False}

    def _boom(*a, **k):
        called["gen"] = True
        raise AssertionError("_feature_run must NOT run when push config is missing")

    monkeypatch.setattr(ds, "_feature_run", _boom)

    with pytest.raises(HTTPException) as exc:
        ds.run(ds.RunRequest(pack="anything", project="app"))
    assert exc.value.status_code == 400
    assert called["gen"] is False


def test_push_config_ok_true_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "_env_token", lambda: "ghp_fake")
    ok, detail = ds._push_config_ok()
    assert ok is True
    assert detail == ""


# ---------------------------------------------- Case 5: explicit --out-dir -> persistent, kept

def test_explicit_out_dir_is_persistent(tmp_path: Path) -> None:
    out = tmp_path / "generated-projects"
    path, is_temp = ds._resolve_out_dir(out, "real")
    assert is_temp is False
    assert path == out.resolve()


def test_finalize_never_deletes_persistent_dir(tmp_path: Path) -> None:
    out = tmp_path / "generated-projects"
    out.mkdir()
    (out / "keep.txt").write_text("keep me", encoding="utf-8")

    # Even on a fully successful run, a user-supplied --out-dir is never auto-deleted.
    removed = ds._finalize_run(out, is_temp=False, success=True)

    assert removed is False
    assert out.exists()
    assert (out / "keep.txt").read_text(encoding="utf-8") == "keep me"


# ------------------------------------------- existing generated-apps contents left untouched

def test_existing_generated_apps_untouched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Simulate a populated generated-apps from previous runs.
    fake_generated_apps = tmp_path / "generated-apps"
    fake_generated_apps.mkdir()
    (fake_generated_apps / "old-project").mkdir()
    (fake_generated_apps / "old-project" / "app.py").write_text("legacy", encoding="utf-8")
    monkeypatch.setattr(fc, "_DEFAULT_OUT_DIR", fake_generated_apps)

    # Resolving an ephemeral dir must not point at, create under, or clear generated-apps.
    path, is_temp = ds._resolve_out_dir(None, "real")
    try:
        assert is_temp is True
        assert fake_generated_apps not in path.parents
        assert path != fake_generated_apps
        # Untouched: the pre-existing project and its file survive.
        assert (fake_generated_apps / "old-project" / "app.py").read_text(encoding="utf-8") == "legacy"
    finally:
        fc._force_rmtree(path)


# ---------------------------------------------------- persistent/dry-run: guard is a no-op

def test_guard_noop_when_persistent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "MODE", "real")
    monkeypatch.setattr(ds, "OUT_DIR_IS_TEMP", False)  # --out-dir given
    monkeypatch.setattr(ds, "_env_token", lambda: "")
    monkeypatch.setattr(ds, "_gh", lambda *a, **k: (1, "", "not logged in"))
    ds._guard_ephemeral_push()  # must not raise — persistent mode never requires push
