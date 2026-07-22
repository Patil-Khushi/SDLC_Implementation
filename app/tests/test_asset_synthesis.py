"""Asset synthesis — the planner turns bulk ``assets/…/`` directory leaves into concrete ``.svg``
targets, normalizes binary favicons to SVG, and the code generator grounds those in the mockup.

Regression target: ``src/assets/images/`` and ``src/assets/icons/`` are directory leaves in the
frontend structure, so ``_source_leaves`` used to drop them and no asset file was ever planned —
every generated ``import x from '@/assets/…'`` dangled and ``favicon.ico`` (binary) was emitted as
corrupt text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.code_generator import _all_svgs, _available_assets
from app.services import design_pack
from app.services.plan_builder import (
    _expand_asset_leaf,
    _normalize_binary_assets,
    _source_leaves,
    build_plan,
)

# app/tests -> app -> SDLC_Implementation -> SDLC(repo) / fixtures
_FIXTURES = Path(__file__).resolve().parents[2].parent / "fixtures"


# --- unit: expansion & normalization (no fixture needed) -------------------

def test_expand_icons_leaf_yields_svgs() -> None:
    leaves = _expand_asset_leaf("src/assets/icons/", "SVG icon files used throughout the UI")
    paths = [p for p, _ in leaves]
    assert paths, "icons folder must expand to concrete files"
    assert all(p.endswith(".svg") for p in paths)
    assert "src/assets/icons/cart.svg" in paths
    assert "src/assets/icons/search.svg" in paths


def test_expand_images_leaf_names_from_description() -> None:
    desc = "Static image assets (logo, placeholder product image, empty-state illustrations)"
    paths = [p for p, _ in _expand_asset_leaf("src/assets/images/", desc)]
    assert paths == [
        "src/assets/images/logo.svg",
        "src/assets/images/placeholder-product.svg",
        "src/assets/images/empty-state.svg",
    ]


def test_expand_images_leaf_falls_back_when_no_names() -> None:
    paths = [p for p, _ in _expand_asset_leaf("src/assets/images/", "Static image assets")]
    assert paths == [
        "src/assets/images/logo.svg",
        "src/assets/images/placeholder.svg",
        "src/assets/images/empty-state.svg",
    ]


def test_normalize_binary_assets_rewrites_favicon_and_index() -> None:
    leaves = [
        ("public/favicon.ico", "Site favicon"),
        ("index.html", "Vite HTML entry; references main.jsx"),
        ("public/robots.txt", "Robots"),
    ]
    out = dict(_normalize_binary_assets(leaves))
    assert "public/favicon.svg" in out
    assert "public/favicon.ico" not in out
    assert 'href="/favicon.svg"' in out["index.html"]


def test_normalize_is_noop_without_favicon() -> None:
    leaves = [("index.html", "Vite HTML entry")]
    assert _normalize_binary_assets(leaves) == leaves  # index.html untouched when there's no favicon


def test_source_leaves_expands_assets_and_normalizes_favicon() -> None:
    tree = {
        "src/": {"assets/": {"icons/": "SVG icons", "images/": "Static image assets (logo)"}},
        "public/": {"favicon.ico": "Site favicon", "robots.txt": "Robots"},
        "index.html": "Vite entry",
    }
    paths = {p for p, _ in _source_leaves(tree)}
    assert any(p.endswith("assets/icons/cart.svg") for p in paths)
    assert "src/assets/images/logo.svg" in paths
    assert "public/favicon.svg" in paths
    assert not any(p.endswith(".ico") for p in paths)
    assert not any(p.endswith("assets/icons/") for p in paths)  # dir leaf itself is not a target


# --- unit: code-generator grounding helpers --------------------------------

def test_all_svgs_extracts_and_dedupes() -> None:
    html = '<div><svg id="a"><path/></svg> text <svg id="b"><circle/></svg><svg id="a"><path/></svg></div>'
    out = _all_svgs(html)
    assert out.count("<svg") == 2  # duplicate collapsed


def test_available_assets_emits_import_paths_from_tree() -> None:
    struct = {"src/": {"assets/": {"icons/": "SVG icons", "images/": "Static image assets (logo)"}}}
    manifest = _available_assets({"frontend-structure.json": struct})
    assert "- @/assets/icons/cart.svg" in manifest
    assert "- @/assets/images/logo.svg" in manifest


# --- integration: the real resources pack ----------------------------------

def _pack_or_skip(name: str) -> Path:
    pack = _FIXTURES / name
    if not (pack.is_dir() and design_pack.is_design_pack(pack)):
        pytest.skip(f"{name} design pack not found at {pack}")
    return pack


def test_resources_plan_has_asset_items_and_no_ico() -> None:
    items = build_plan(_pack_or_skip("resources"))
    ids = {it.id for it in items}
    assert "frontend-assets-icons" in ids
    assert "frontend-assets-images" in ids
    asset_targets = [f for it in items if it.id.startswith("frontend-assets") for f in it.target_files]
    assert asset_targets and all(f.endswith(".svg") for f in asset_targets)
    assert not any(f.lower().endswith(".ico") for it in items for f in it.target_files)


def test_legacy_pack_gets_no_asset_items() -> None:
    """A legacy pack with no ``assets/`` folder must be untouched by asset synthesis."""
    items = build_plan(_pack_or_skip("authentication"))
    assert not any(it.id.startswith("frontend-assets") for it in items)
    assert not any(f.lower().endswith(".ico") for it in items for f in it.target_files)
