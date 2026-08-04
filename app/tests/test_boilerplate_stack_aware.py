"""Stack-aware, per-project scaffold (Phase 1a/1b).

Pins the NEW behaviour on top of ``test_boilerplate.py`` (which still guards the legacy
Python+React default byte-for-byte): a Node backend gets a Node Dockerfile + a real backend
``package.json`` (npm deps + start/migrate/seed scripts) instead of a Python
Dockerfile+requirements.txt; and when the structure trees are separated into wrapper dirs, each
side's manifest lands inside its own project root.
"""

from __future__ import annotations

import json

from app.services.boilerplate import render_scaffold, resolve_scaffold_config

# A separated Node/React pack: distinct backend/frontend wrapper dirs, JS backend files.
SEPARATED_PACK = {
    "backend-structure.json": {
        "tree": {
            "quickbite-backend/": {
                "src/": {
                    "server.js": "http server entrypoint",
                    "app.js": "express app factory",
                    "modules/": {"orders/": {"orders.service.js": "orders service"}},
                },
            },
        }
    },
    "frontend-structure.json": {
        "tree": {
            "quickbite-frontend/": {
                "src/": {"App.jsx": "root component", "main.jsx": "entry"},
            },
        }
    },
}


def _by_path(project: str, pack: dict) -> dict[str, str]:
    return {f["path"]: f["content"] for f in render_scaffold(project, pack)}


def test_node_backend_inferred_from_structure_tree() -> None:
    cfg = resolve_scaffold_config("quickbite", SEPARATED_PACK)
    assert cfg.backend_language == "node"
    assert cfg.option("backend", "framework") == "express"
    # No rich_api_mapping CSV in SEPARATED_PACK -> app.services.plan_builder's ADAPTIVE path
    # re-roots every generated file under canonical backend/ / frontend/ folders regardless of
    # what the structure trees themselves are wrapped in (plan_builder._reroot discards a wrapper
    # name like "quickbite-backend/" outright) — so the scaffold must place its own manifest /
    # Dockerfile / jest harness there too, or their rootDir-relative paths (e.g. jest's "@/" alias)
    # point at a namespace nothing was ever generated under. See design_pack.has_rich_api_mapping.
    assert cfg.backend_root == "backend/"
    assert cfg.frontend_root == "frontend/"


def test_separated_pack_emits_per_project_manifests() -> None:
    files = _by_path("quickbite", SEPARATED_PACK)

    # No Python artifacts at all for a Node backend.
    assert "requirements.txt" not in files
    assert not any(p.endswith("requirements.txt") for p in files)

    # Backend manifest + Dockerfile live inside the CANONICAL backend project root (not the
    # pack's own "quickbite-backend/" wrapper name — see test_node_backend_inferred_from_structure_tree).
    assert "backend/package.json" in files
    assert "backend/Dockerfile" in files
    assert "node:20-slim" in files["backend/Dockerfile"]

    # Frontend manifest lives inside the canonical frontend project root.
    assert "frontend/package.json" in files
    # ... and NOT at the repo root (that was the single-shared-tree bug) ...
    assert "package.json" not in files
    # ... nor under the pack's own wrapper names, which build_plan never uses for generated code.
    assert "quickbite-backend/package.json" not in files
    assert "quickbite-frontend/package.json" not in files


def test_backend_package_json_has_real_node_deps_and_scripts() -> None:
    files = _by_path("quickbite", SEPARATED_PACK)
    pkg = json.loads(files["backend/package.json"])

    assert pkg["name"] == "quickbite-backend"
    assert "express" in pkg["dependencies"]
    assert "knex" in pkg["dependencies"] and "pg" in pkg["dependencies"]  # postgres default
    assert "bcryptjs" in pkg["dependencies"]        # pure-JS hash (issue 4d)
    assert "bcrypt" not in pkg["dependencies"]       # never the native module
    assert pkg["scripts"]["start"] == "node src/server.js"
    assert pkg["scripts"]["migrate"] == "knex migrate:latest"
    assert pkg["scripts"]["seed"] == "knex seed:run"


def test_backend_env_example_has_node_backend_vars() -> None:
    env_file = _by_path("quickbite", SEPARATED_PACK)[".env.example"]
    for var in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME", "JWT_SECRET"):
        assert f"{var}=" in env_file
    assert "DATABASE_URL" not in env_file  # that's the Python convention


def test_shared_root_node_pack_collapses_to_one_combined_manifest() -> None:
    # Both trees rooted at src/ (no wrapper) — a Node app can't carry two package.json at the root,
    # so the scaffold emits ONE combined manifest carrying both frontend and backend deps.
    #
    # Carries a rich_api_mapping CSV on purpose: WITHOUT one, app.services.plan_builder treats this
    # as an ADAPTIVE pack and unconditionally re-roots backend+frontend under canonical, DIFFERENT
    # backend/ / frontend/ folders regardless of the trees' own (wrapper-less) shape — so it would
    # never actually collapse to one manifest once combined with build_plan's real behavior (see
    # design_pack.has_rich_api_mapping). Only the LEGACY path keeps paths byte-for-byte, so only a
    # legacy-shaped pack can genuinely reach the shared-root, single-manifest scenario this test
    # protects.
    shared = {
        "backend-structure.json": {"tree": {"src/": {"server.js": "entry", "app.js": "factory"}}},
        "frontend-structure.json": {"tree": {"src/": {"App.jsx": "root"}}},
        "api-to-ui-mapping.csv": "operation_id,endpoint_path,req_ids\nloginUser,/api/login,REQ-1\n",
    }
    files = _by_path("resource-app", shared)
    assert "requirements.txt" not in files
    assert list(p for p in files if p.endswith("package.json")) == ["package.json"]
    pkg = json.loads(files["package.json"])
    assert "express" in pkg["dependencies"]          # backend deps present
    assert "react" in pkg["dependencies"]            # frontend deps present


def test_adaptive_shared_tree_pack_still_gets_separate_manifests() -> None:
    # Same wrapper-less trees as the legacy test above, but WITHOUT a rich_api_mapping CSV: this is
    # exactly the shape app.services.plan_builder's ADAPTIVE path re-roots to canonical, DIFFERENT
    # backend/ / frontend/ folders (plan_builder._reroot) — so, unlike the legacy case, the two
    # sides must NOT collapse into one manifest, or the scaffold's package.json/jest harness would
    # sit at a path (the repo root) nothing is actually generated under.
    shared_adaptive = {
        "backend-structure.json": {"tree": {"src/": {"server.js": "entry", "app.js": "factory"}}},
        "frontend-structure.json": {"tree": {"src/": {"App.jsx": "root"}}},
    }
    files = _by_path("resource-app", shared_adaptive)
    assert "package.json" not in files
    assert "backend/package.json" in files
    assert "frontend/package.json" in files


def test_explicit_python_framework_still_wins_over_inference() -> None:
    # A JS-looking tree but capabilities explicitly say fastapi -> stays Python (explicit wins).
    pack = dict(SEPARATED_PACK)
    pack["capabilities"] = {"backend": {"framework": "fastapi"}}
    cfg = resolve_scaffold_config("x", pack)
    assert cfg.backend_language == "python"
