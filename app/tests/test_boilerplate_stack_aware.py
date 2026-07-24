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
    assert cfg.backend_root == "quickbite-backend/"
    assert cfg.frontend_root == "quickbite-frontend/"


def test_separated_pack_emits_per_project_manifests() -> None:
    files = _by_path("quickbite", SEPARATED_PACK)

    # No Python artifacts at all for a Node backend.
    assert "requirements.txt" not in files
    assert not any(p.endswith("requirements.txt") for p in files)

    # Backend manifest + Dockerfile live inside the backend project root.
    assert "quickbite-backend/package.json" in files
    assert "quickbite-backend/Dockerfile" in files
    assert "node:20-slim" in files["quickbite-backend/Dockerfile"]

    # Frontend manifest lives inside the frontend project root.
    assert "quickbite-frontend/package.json" in files
    # ... and NOT at the repo root (that was the single-shared-tree bug).
    assert "package.json" not in files


def test_backend_package_json_has_real_node_deps_and_scripts() -> None:
    files = _by_path("quickbite", SEPARATED_PACK)
    pkg = json.loads(files["quickbite-backend/package.json"])

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
    shared = {
        "backend-structure.json": {"tree": {"src/": {"server.js": "entry", "app.js": "factory"}}},
        "frontend-structure.json": {"tree": {"src/": {"App.jsx": "root"}}},
    }
    files = _by_path("resource-app", shared)
    assert "requirements.txt" not in files
    assert list(p for p in files if p.endswith("package.json")) == ["package.json"]
    pkg = json.loads(files["package.json"])
    assert "express" in pkg["dependencies"]          # backend deps present
    assert "react" in pkg["dependencies"]            # frontend deps present


def test_explicit_python_framework_still_wins_over_inference() -> None:
    # A JS-looking tree but capabilities explicitly say fastapi -> stays Python (explicit wins).
    pack = dict(SEPARATED_PACK)
    pack["capabilities"] = {"backend": {"framework": "fastapi"}}
    cfg = resolve_scaffold_config("x", pack)
    assert cfg.backend_language == "python"
