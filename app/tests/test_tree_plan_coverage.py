"""Tree-driven plan coverage — regression tests for the QuickBite under-generation bug.

Before the fix the adaptive plan was driven off endpoints/screens, so cross-cutting/bootstrap
files (server/app entrypoints, config, middleware, routers, stores, pages, hooks) and
endpoint-less modules were never planned, and per-endpoint items clobbered shared module files.
These tests pin the new guarantee: EVERY non-test file leaf in the structure trees is produced
by some work item, one item per module/directory.

Uses the real ``fixtures/Test`` (QuickBite) pack via an absolute path, so it does not depend on
the conftest ``FIXTURES_DIR`` resolution (which overshoots in this flattened checkout).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import design_pack
from app.services.design_pack import _tokens
from app.services.plan_builder import _source_leaves, build_plan

# app/tests -> app -> SDLC_Implementation -> SDLC(repo) / fixtures / Test
_TEST_PACK = Path(__file__).resolve().parents[2].parent / "fixtures" / "Test"


def _pack_or_skip() -> Path:
    if not (_TEST_PACK.is_dir() and design_pack.is_design_pack(_TEST_PACK)):
        pytest.skip(f"QuickBite design pack not found at {_TEST_PACK}")
    return _TEST_PACK


# --- tokenizer -------------------------------------------------------------

def test_tokens_splits_camelcase() -> None:
    # The root-cause bug: "LoginPage" collapsed to {"loginpage"}, so page matching never fired.
    assert _tokens("LoginPage") == {"login", "page"}
    assert _tokens("RestaurantListPage") == {"restaurant", "list", "page"}
    assert _tokens("Customer Login") == {"customer", "login"}


# --- full-coverage guarantee ----------------------------------------------

def test_plan_covers_every_structure_leaf() -> None:
    pack = _pack_or_skip()
    items = build_plan(pack)
    resolved = design_pack.resolve(pack)

    leaves = {p for p, _ in _source_leaves(resolved.backend_tree)}
    leaves |= {p for p, _ in _source_leaves(resolved.frontend_tree)}
    covered = {f for item in items for f in item.target_files}

    missing = sorted(leaves - covered)
    assert not missing, f"{len(missing)} structure-tree files not planned: {missing[:10]}"


def test_bootstrap_and_ui_files_are_planned() -> None:
    """Exactly the file classes that were missing from the pushed QuickBite repo."""
    pack = _pack_or_skip()
    covered = {f for item in build_plan(pack) for f in item.target_files}
    for path in [
        "quickbite-backend/src/app.js",
        "quickbite-backend/src/server.js",
        "quickbite-backend/src/config/db.js",
        "quickbite-backend/src/middleware/errorHandler.js",
        "quickbite-backend/src/routes/index.js",
        "quickbite-backend/src/modules/notifications/notifications.service.js",
        "quickbite-frontend/src/index.jsx",
        "quickbite-frontend/src/App.jsx",
        "quickbite-frontend/src/pages/auth/LoginPage/LoginPage.jsx",
        "quickbite-frontend/src/store/index.js",
        "quickbite-frontend/src/hooks/useAuth.js",
    ]:
        assert path in covered, f"{path} was not planned"


def test_module_item_bundles_files_without_clobber() -> None:
    """A module is ONE item owning all its files with every endpoint attached — not N clobbering
    per-endpoint items."""
    pack = _pack_or_skip()
    items = build_plan(pack)
    orders = next((it for it in items if it.id == "backend-modules-orders"), None)
    assert orders is not None, "expected a single backend-modules-orders item"
    # all sibling module files in one item
    assert any(f.endswith("orders.controller.js") for f in orders.target_files)
    assert any(f.endswith("orders.service.js") for f in orders.target_files)
    assert any(f.endswith("orders.routes.js") for f in orders.target_files)
    # multiple endpoints aggregated onto the one item (was: one endpoint each, overwriting)
    assert len(orders.endpoints) > 1
    # file_specs carries the design-tree description for each target file
    assert orders.file_specs and set(orders.file_specs) == set(orders.target_files)


def test_no_duplicate_work_item_ids() -> None:
    pack = _pack_or_skip()
    ids = [it.id for it in build_plan(pack)]
    assert len(ids) == len(set(ids)), "work item ids must be unique"
