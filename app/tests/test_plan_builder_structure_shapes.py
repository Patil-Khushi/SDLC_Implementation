"""Regression: two compounding bugs that silently produced ZERO frontend work items for a real
design pack (``fixtures/authentication``), even though it had a complete frontend-structure.json.

1. ``_structure_obj`` (used by both ``_backend_items``/``_frontend_items``) assumed every structure
   file is wrapped as ``{"tree": {...}, "notes": ...}``. This pack's ``backend-structure.json`` was
   wrapped that way, but its OWN ``frontend-structure.json`` put the tree directly at the top level
   (``{"auth-frontend/": {...}}``, no ``"tree"`` key) — a real, in-the-wild inconsistency between two
   files in the SAME hand-off. Reading ``{}.get("tree", {})`` off the un-normalized object silently
   returned zero leaves. Fixed: ``_structure_obj`` now wraps a bare tree automatically.

2. Even after (1), ``_frontend_items``'s route matching was too fragile for realistic data: a page
   leaf's description is "route /login: email + password, remember me" (route + a free-text
   explanation after a colon), but the old key extraction (``str(desc).replace("route", "").strip()``)
   kept everything after the word "route", including the colon and explanation — and the CSV's
   ``route_id`` column is a bare word ("login", no leading slash) while the description embeds
   "/login" (with one). Neither the colon-suffixed text nor the slash-prefixed one ever matched the
   bare CSV value. Fixed: a regex captures just the path token, and both sides are normalized
   (leading/trailing "/" stripped) before comparing.

Together these meant: build_plan() on this fixture returned all 7 backend work items but 0 of the
5 frontend ones — the Code Generator only ever saw backend targets, so only backend files were
ever written, with no error or warning to explain why.

3. A THIRD, deeper gap surfaced once (1) and (2) were fixed: even with all 12 per-operation/
   per-screen items present, only 10 of the pack's 61 structure-tree files were targeted by ANY
   item. The legacy builders (``_backend_items``/``_frontend_items``) only ever produce ONE item
   per operation/screen, with target files tied to THAT operation — so shared/cross-cutting
   infrastructure (``main.py``, ``config/settings.py``, ``core/security.py``, a frontend
   ``App.tsx``, ``api/client.ts``, shared form components, ...) is never assigned to anything and
   silently never generated. The adaptive builders already had a guard for exactly this
   ("authoritative-manifest guarantee" — ``_reconcile_uncovered``), but it was only wired into the
   adaptive path. Fixed: ``_reconcile_uncovered`` now takes plain trees (not a ``ResolvedPack``) so
   the legacy path can reuse the SAME sweep instead of having no guard at all.

4. An early version of the (3) fix added a warning whenever ``_backend_items``/``_frontend_items``
   produced ZERO items for a detected structure role — which sounded reasonable but was a false
   alarm: ``fixtures/tic-tac-toe`` legitimately has NO backend REST operations (every CSV row's
   ``operation_id`` is ``"-"``, noted "client-side only, no API"), so 0 per-operation items is
   CORRECT there, not a bug — every backend file still lands in the plan via the reconcile sweep.
   Fixed: the warning now checks FINAL coverage after reconciliation (``_missing_after_reconcile``),
   not the intermediate per-operation builder output, so it only fires on a genuine, unexplained gap.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from app.models import WorkItem
from app.services.design_pack import DetectedFile
from app.services.plan_builder import (
    _backend_items,
    _frontend_items,
    _missing_after_reconcile,
    _reconcile_uncovered,
    _route_key,
    _structure_obj,
    build_plan,
)

_WRAPPED_BACKEND_TREE = {
    "auth-backend/": {"app/": {"auth/": {"router.py": "route handlers"}}},
}
# Realistic page description: "route /login" followed by a free-text explanation after a colon —
# exactly the shape that broke the old "strip the word route" key extraction.
_BARE_FRONTEND_TREE = {
    "auth-frontend/": {"src/": {"pages/": {
        "Login.tsx": "route /login: email + password, remember me",
    }}},
}
# The CSV convention actually used in the fixture: a bare route_id, no leading slash.
_ROW = {
    "operation_id": "login", "endpoint_path": "/auth/login", "http_method": "POST",
    "req_ids": "REQ-001", "route_id": "login", "screen": "Login",
}


def _roles_with(role: str, obj: dict) -> dict:
    return {role: [DetectedFile(path=Path(f"{role}.json"), role=role, obj=obj)]}


# --------------------------------------------------------------------------- bug 1: _structure_obj


def test_structure_obj_passes_through_an_already_wrapped_tree() -> None:
    wrapped = {"tree": _WRAPPED_BACKEND_TREE, "notes": "x"}
    roles = _roles_with("backend_structure", wrapped)
    assert _structure_obj(roles, "backend_structure") == wrapped


def test_structure_obj_wraps_a_bare_tree_with_no_wrapper_key() -> None:
    roles = _roles_with("frontend_structure", _BARE_FRONTEND_TREE)
    result = _structure_obj(roles, "frontend_structure")
    assert result == {"tree": _BARE_FRONTEND_TREE}


def test_structure_obj_returns_empty_for_a_missing_role() -> None:
    assert _structure_obj({}, "frontend_structure") == {}


# --------------------------------------------------------------------------- bug 2: route matching


def test_route_key_normalizes_leading_and_trailing_slash() -> None:
    assert _route_key("/login") == "login"
    assert _route_key("login") == "login"
    assert _route_key("/login/") == "login"


def test_route_description_with_trailing_explanation_matches_a_bare_csv_route_id() -> None:
    frontend = {"tree": _BARE_FRONTEND_TREE}
    items = _frontend_items([_ROW], frontend)
    assert len(items) == 1
    assert items[0].id == "frontend-login"
    assert items[0].target_files == ["auth-frontend/src/pages/Login.tsx"]


# --------------------------------------------------------------------------- both, end-to-end


def test_frontend_items_are_produced_from_a_bare_unwrapped_tree() -> None:
    # Before the fix: _frontend_items(rows, {"auth-frontend/": {...}}) walked `{}.get("tree", {})`
    # and silently returned []. After: _structure_obj wraps it first, so the leaf is found.
    frontend = _structure_obj(_roles_with("frontend_structure", _BARE_FRONTEND_TREE), "frontend_structure")
    items = _frontend_items([_ROW], frontend)
    assert len(items) == 1
    assert items[0].id == "frontend-login"


def test_build_plan_generates_frontend_items_when_frontend_structure_is_unwrapped(tmp_path: Path) -> None:
    """Full build_plan() through a real pack directory — the exact shape mismatch from production:
    backend-structure.json wrapped in {"tree": ...}, frontend-structure.json a bare tree, and a
    bare (slash-less) route_id column alongside a colon-suffixed page description."""
    csv_text = io.StringIO()
    writer = csv.DictWriter(csv_text, fieldnames=list(_ROW.keys()))
    writer.writeheader()
    writer.writerow(_ROW)
    (tmp_path / "api-mapping.csv").write_text(csv_text.getvalue(), encoding="utf-8")
    (tmp_path / "backend-structure.json").write_text(
        json.dumps({"tree": _WRAPPED_BACKEND_TREE}), encoding="utf-8"
    )
    (tmp_path / "frontend-structure.json").write_text(
        json.dumps(_BARE_FRONTEND_TREE), encoding="utf-8"  # deliberately NO "tree" wrapper
    )

    items = build_plan(tmp_path)

    backend_ids = [i.id for i in items if i.id.startswith("backend-")]
    frontend_ids = [i.id for i in items if i.id.startswith("frontend-")]
    assert "backend-login" in backend_ids
    assert "frontend-login" in frontend_ids  # the bug: this used to be missing entirely

    # Every non-test leaf in BOTH structure trees ends up targeted by some item — the per-op/
    # per-screen items, or (for anything they don't claim, e.g. this synthetic router.py, which
    # isn't classified as this op's handler by name) the reconcile-uncovered catch-all sweep.
    targeted = {f for item in items for f in item.target_files}
    assert "auth-backend/app/auth/router.py" in targeted
    assert "auth-frontend/src/pages/Login.tsx" in targeted


# --------------------------------------------------------------------------- bug 3: shared/cross-
# cutting files (main.py, config, ...) that no per-operation item ever claims


def test_reconcile_uncovered_sweeps_shared_files_the_legacy_builders_never_target() -> None:
    # A realistic shape: one operation-specific handler (claimed by the "login" item below) PLUS
    # app-wide infrastructure files that belong to no single operation — main.py, config/settings.py
    # — which the per-operation builder has no way to ever assign to any item on its own.
    backend_tree = {
        "auth-backend/": {
            "app/": {
                "main.py": "FastAPI app bootstrap",
                "config/": {"settings.py": "typed env config"},
                "auth/": {"router.py": "login: POST /auth/login"},
            },
        },
    }
    rows = [{
        "operation_id": "login", "endpoint_path": "/auth/login", "http_method": "POST",
        "req_ids": "REQ-001", "route_id": "login", "screen": "Login",
    }]
    items = _backend_items(rows, tables=[], backend={"tree": backend_tree})
    targeted_before = {f for item in items for f in item.target_files}
    assert "auth-backend/app/main.py" not in targeted_before  # confirms the gap exists pre-sweep
    assert "auth-backend/app/config/settings.py" not in targeted_before

    items += _reconcile_uncovered(backend_tree, {}, items)
    targeted_after = {f for item in items for f in item.target_files}
    assert "auth-backend/app/main.py" in targeted_after
    assert "auth-backend/app/config/settings.py" in targeted_after


# --------------------------------------------------------------------------- bug 4: false-positive
# "0 work items" warning when a pack legitimately has no backend operations


def test_no_operations_but_full_leaf_coverage_reports_nothing_missing() -> None:
    # fixtures/tic-tac-toe's real shape: every CSV row's operation_id is "-" (no backend REST
    # surface at all — a client-only game). _backend_items() correctly returns 0 items for that;
    # the completeness check must judge FINAL coverage, not treat 0 per-operation items as a gap.
    backend_tree = {"game-backend/": {"server.py": "static file server, no REST endpoints"}}
    rows = [{
        "operation_id": "-", "endpoint_path": "-", "http_method": "-",
        "req_ids": "FR-01", "route_id": "game", "screen": "Game",
    }]
    items = _backend_items(rows, tables=[], backend={"tree": backend_tree})
    assert items == []  # confirms this pack genuinely has no per-operation backend items

    items += _reconcile_uncovered(backend_tree, {}, items)
    assert _missing_after_reconcile(backend_tree, {}, items) == []


def test_missing_after_reconcile_detects_a_leaf_neither_stage_covered() -> None:
    # A leaf that never went through _reconcile_uncovered at all (simulating some future edit that
    # forgets to sweep a tree) must be caught by name, not silently ignored.
    backend_tree = {"app/": {"orphan.py": "nobody claims this file"}}
    items: list[WorkItem] = [WorkItem(id="backend-x", target_files=["app/other.py"])]
    assert _missing_after_reconcile(backend_tree, {}, items) == ["app/orphan.py"]
