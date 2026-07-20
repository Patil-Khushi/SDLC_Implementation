"""Unit tests for the deterministic, per-application README generator (no LLM)."""

import json

from app.services.boilerplate import render_scaffold, resolve_scaffold_config
from app.services.readme import render_readme

# A rich, coherent design package mirroring the `authentication` fixture's shapes.
AUTH_CAPS = {
    "project": {
        "name": "auth-starter",
        "description": "Email + password authentication starter — React + FastAPI + PostgreSQL",
        "version": "1.0.0",
    },
}
AUTH_PACKAGE = {
    "capabilities.yaml": json.dumps(AUTH_CAPS),  # JSON is valid YAML; exercises the string path
    "user_features.json": {
        "roles": [
            {"role": "Visitor (Guest)", "description": "An unauthenticated person."},
            {"role": "Registered User", "description": "A person with an account."},
        ],
        "entities": ["User", "Refresh Token"],
        "features": [
            {"id": "US-01", "name": "Register an account", "description": "Create an account.",
             "requirements": ["FR-01"]},
            {"id": "US-02", "name": "Log in", "description": "Sign in with email and password.",
             "requirements": ["FR-02"]},
        ],
    },
    "routes.json": {"Login": "/login", "Register": "/register", "Profile": "/profile"},
    "openapi.yaml": (
        "openapi: 3.0.0\n"
        "paths:\n"
        "  /auth/login:\n"
        "    post:\n"
        "      summary: Authenticate a user\n"
        "  /auth/me:\n"
        "    get:\n"
        "      summary: Current user\n"
    ),
    "schema.sql": (
        "CREATE TABLE users (id SERIAL PRIMARY KEY);\n"
        "CREATE TABLE refresh_tokens (id SERIAL PRIMARY KEY);\n"
    ),
    "backend-structure.json": {
        "tree": {"auth-backend/": {"app/": {"main.py": "FastAPI app entrypoint"}}},
        "notes": ["Endpoints match openapi.yaml exactly."],
    },
    "frontend-structure.json": {
        "auth-frontend/": {"src/": {"App.tsx": "route table"}},
    },
}


def _readme(project: str, package: dict) -> str:
    return {f["path"]: f["content"] for f in render_scaffold(project, package)}["README.md"]


# --- Backward compatibility ---------------------------------------------------------------------

def test_sparse_package_still_has_title_and_description() -> None:
    caps = {"capabilities": {"project": {"name": "acme", "description": "A tiny app"}}}
    readme = _readme("run-1", caps)
    assert readme.startswith("# acme")
    assert "A tiny app" in readme


def test_bare_render_has_no_data_sections() -> None:
    # No design package at all -> degrade to title + stack + getting-started, no Features/API/etc.
    readme = _readme("acme", {})
    assert readme.startswith("# acme")
    assert "## Features" not in readme
    assert "## Project structure" not in readme
    assert "## API reference" not in readme


def test_render_is_deterministic() -> None:
    assert _readme("auth-starter", AUTH_PACKAGE) == _readme("auth-starter", AUTH_PACKAGE)


# --- Rich, application-specific content ----------------------------------------------------------

def test_features_section_lists_each_feature() -> None:
    readme = _readme("auth-starter", AUTH_PACKAGE)
    assert "## Features" in readme
    assert "Register an account" in readme
    assert "Log in" in readme
    assert "### User roles" in readme
    assert "Registered User" in readme


def test_api_section_lists_endpoints_from_openapi() -> None:
    readme = _readme("auth-starter", AUTH_PACKAGE)
    assert "## API reference" in readme
    assert "`POST`" in readme and "`/auth/login`" in readme
    assert "Authenticate a user" in readme
    assert "`/auth/me`" in readme


def test_routes_section_lists_screens() -> None:
    readme = _readme("auth-starter", AUTH_PACKAGE)
    assert "## Screens & routes" in readme
    assert "/login" in readme and "/profile" in readme


def test_data_model_section_lists_tables() -> None:
    readme = _readme("auth-starter", AUTH_PACKAGE)
    assert "## Data model" in readme
    assert "`users`" in readme
    assert "`refresh_tokens`" in readme


def test_structure_section_renders_the_file_tree() -> None:
    readme = _readme("auth-starter", AUTH_PACKAGE)
    assert "## Project structure" in readme
    assert "auth-backend/" in readme
    assert "main.py - FastAPI app entrypoint" in readme
    assert "auth-frontend/" in readme


def test_notes_section_carries_structure_notes() -> None:
    readme = _readme("auth-starter", AUTH_PACKAGE)
    assert "## Notes" in readme
    assert "Endpoints match openapi.yaml exactly." in readme


def test_tech_stack_reflects_capabilities() -> None:
    readme = _readme("auth-starter", AUTH_PACKAGE)
    assert "## Tech stack" in readme
    assert "Fastapi" in readme  # backend framework, capitalized
    assert "Postgres" in readme  # database provider


# --- Frontend-only project trims backend/db/docker sections -------------------------------------

def test_frontend_only_omits_backend_sections() -> None:
    caps = {
        "capabilities": {
            "project": {"name": "tic-tac-toe", "description": "Two-player 3x3 game"},
            "frontend": {"enabled": True}, "backend": {"enabled": False},
            "database": {"enabled": False}, "docker": {"enabled": False},
            "environment": {"enabled": False}, "authentication": {"enabled": False},
        }
    }
    readme = _readme("ttt", caps)
    assert "Two-player 3x3 game" in readme
    assert "## API reference" not in readme  # backend disabled
    assert "## Data model" not in readme     # database disabled
    assert "uvicorn" not in readme           # no backend run command


# --- render_readme is usable directly with a resolved config ------------------------------------

def test_render_readme_direct_with_config() -> None:
    config = resolve_scaffold_config("auth-starter", {"capabilities.yaml": json.dumps(AUTH_CAPS)})
    readme = render_readme(config, AUTH_PACKAGE)
    assert readme.startswith("# auth-starter")
    assert "## Features" in readme
