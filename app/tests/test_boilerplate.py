"""Unit tests for the deterministic project-scaffold renderer (no LLM, no executor)."""

import json

import yaml

from app.services.boilerplate import render_scaffold, resolve_scaffold_config

# Legacy full-stack scaffold (no capabilities artifact -> defaults). Order is significant.
EXPECTED_PATHS = [
    "Dockerfile",
    ".gitignore",
    "README.md",
    "docker-compose.yml",
    ".env.example",
    "requirements.txt",
    "package.json",
]

# The Tic-Tac-Toe fixture: a static React SPA — no backend, db, docker, or env.
TTT_CAPABILITIES = {
    "project": {"name": "tic-tac-toe"},
    "frontend": {"enabled": True, "framework": "react", "bundler": "vite"},
    "backend": {"enabled": False},
    "database": {"enabled": False},
    "authentication": {"enabled": False},
    "docker": {"enabled": False},
    "environment": {"enabled": False},
    "testing": {"enabled": True},
}


# --- Backward compatibility (no capabilities artifact -> legacy defaults) -----------------------

def test_renders_the_fixed_set_of_boilerplate_files() -> None:
    files = render_scaffold("acme")

    assert [f["path"] for f in files] == EXPECTED_PATHS
    assert all(f["content"].strip() for f in files)  # nothing rendered empty


def test_rendering_is_deterministic_for_the_same_project_id() -> None:
    assert render_scaffold("acme") == render_scaffold("acme")


def test_project_id_is_interpolated_where_expected() -> None:
    files = {f["path"]: f["content"] for f in render_scaffold("acme")}

    assert "acme" in files["README.md"]
    assert "acme" in files["Dockerfile"]
    assert "acme-frontend" in files["package.json"]


def test_empty_design_package_falls_back_to_defaults() -> None:
    assert render_scaffold("acme", {}) == render_scaffold("acme")


# --- Capability-driven skipping ----------------------------------------------------------------

def test_frontend_only_skips_backend_docker_and_env_files() -> None:
    files = {f["path"]: f["content"] for f in render_scaffold("ttt", {"capabilities": TTT_CAPABILITIES})}

    assert set(files) == {".gitignore", "README.md", "package.json"}
    # Skipped files are absent entirely, not emitted empty.
    for absent in ("Dockerfile", "docker-compose.yml", ".env.example", "requirements.txt"):
        assert absent not in files


def test_disabled_capabilities_are_skipped_individually() -> None:
    pkg = {"capabilities": {"docker": {"enabled": False}, "environment": {"enabled": False}}}
    paths = [f["path"] for f in render_scaffold("acme", pkg)]

    assert "docker-compose.yml" not in paths
    assert "Dockerfile" not in paths  # Dockerfile needs docker AND backend
    assert ".env.example" not in paths
    assert "requirements.txt" in paths  # backend still on
    assert "package.json" in paths


def test_capabilities_accepted_as_yaml_string() -> None:
    yaml_pkg = {"capabilities.yaml": yaml.safe_dump(TTT_CAPABILITIES)}
    assert set(f["path"] for f in render_scaffold("ttt", yaml_pkg)) == {
        ".gitignore",
        "README.md",
        "package.json",
    }


def test_capabilities_accepted_as_json_string() -> None:
    json_pkg = {"scaffold.json": json.dumps(TTT_CAPABILITIES)}
    assert set(f["path"] for f in render_scaffold("ttt", json_pkg)) == {
        ".gitignore",
        "README.md",
        "package.json",
    }


# --- Content is capability-aware ----------------------------------------------------------------

def test_project_name_from_capabilities_wins_over_dir() -> None:
    files = {f["path"]: f["content"] for f in render_scaffold("run-123", {"capabilities": TTT_CAPABILITIES})}
    assert "tic-tac-toe-frontend" in files["package.json"]


def test_frontend_only_package_json_is_valid_json() -> None:
    files = {f["path"]: f["content"] for f in render_scaffold("ttt", {"capabilities": TTT_CAPABILITIES})}
    parsed = json.loads(files["package.json"])
    assert parsed["name"] == "tic-tac-toe-frontend"
    assert "test" in parsed["scripts"]  # testing enabled


# --- Real application values (not just placeholders) --------------------------------------------

def test_real_project_values_flow_into_output() -> None:
    caps = {
        "capabilities": {
            "project": {"name": "tic-tac-toe", "description": "Two-player 3x3 game", "version": "1.0.0"},
            "backend": {"enabled": False}, "database": {"enabled": False},
            "docker": {"enabled": False}, "environment": {"enabled": False},
            "authentication": {"enabled": False},
        }
    }
    files = {f["path"]: f["content"] for f in render_scaffold("ttt", caps)}
    pkg = json.loads(files["package.json"])

    assert pkg["version"] == "1.0.0"
    assert pkg["description"] == "Two-player 3x3 game"
    assert "Two-player 3x3 game" in files["README.md"]
    # && is NOT HTML-escaped in the rendered output.
    assert "&&" in pkg["scripts"]["build"]
    assert "\\u0026" not in files["package.json"]


def test_design_package_dependencies_extend_the_baseline() -> None:
    caps = {"capabilities": {"frontend": {"dependencies": {"react-router-dom": "^6.26.0"}},
                             "backend": {"enabled": False}, "docker": {"enabled": False},
                             "database": {"enabled": False}, "environment": {"enabled": False},
                             "authentication": {"enabled": False}}}
    pkg = json.loads({f["path"]: f["content"] for f in render_scaffold("app", caps)}["package.json"])
    # baseline survives ...
    assert pkg["dependencies"]["react"] == "^18.3.0"
    # ... and the design package's real dependency is present.
    assert pkg["dependencies"]["react-router-dom"] == "^6.26.0"


def test_backend_requirements_reflect_capabilities() -> None:
    caps = {"capabilities": {"frontend": {"enabled": False}, "backend": {"framework": "fastapi"},
                             "database": {"enabled": True, "provider": "postgres"},
                             "authentication": {"enabled": True}, "testing": {"enabled": True}}}
    reqs = {f["path"]: f["content"] for f in render_scaffold("api", caps)}["requirements.txt"]

    assert "fastapi>=0.115,<1.0" in reqs
    assert "psycopg[binary]>=3.2,<4.0" in reqs  # postgres
    assert "passlib[bcrypt]>=1.7,<2.0" in reqs  # auth
    assert "pytest>=8.3,<9.0" in reqs           # testing


def test_env_variables_are_real_names_from_the_package() -> None:
    caps = {"capabilities": {"environment": {"enabled": True, "variables": ["API_BASE_URL", "FEATURE_FLAGS"]},
                             "database": {"enabled": False}, "authentication": {"enabled": False}}}
    env_file = {f["path"]: f["content"] for f in render_scaffold("app", caps)}[".env.example"]

    assert "API_BASE_URL=" in env_file
    assert "FEATURE_FLAGS=" in env_file
    assert "DATABASE_URL=" not in env_file  # db disabled, not derived


def test_flask_backend_swaps_dependencies_and_cmd() -> None:
    pkg = {"capabilities": {"frontend": {"enabled": False}, "backend": {"framework": "flask"}}}
    files = {f["path"]: f["content"] for f in render_scaffold("svc", pkg)}

    assert "flask" in files["requirements.txt"]
    assert "fastapi" not in files["requirements.txt"]
    assert "gunicorn" in files["Dockerfile"]
    assert "package.json" not in files  # frontend disabled


def test_env_example_notes_when_no_vars_needed() -> None:
    pkg = {"capabilities": {"database": {"enabled": False}, "authentication": {"enabled": False}}}
    files = {f["path"]: f["content"] for f in render_scaffold("acme", pkg)}
    assert "DATABASE_URL" not in files[".env.example"]
    assert "SECRET_KEY" not in files[".env.example"]
    assert "No environment variables" in files[".env.example"]


# --- Determinism with a design package ----------------------------------------------------------

def test_rendering_is_deterministic_with_a_design_package() -> None:
    pkg = {"capabilities": TTT_CAPABILITIES}
    assert render_scaffold("ttt", pkg) == render_scaffold("ttt", pkg)


# --- Config resolution --------------------------------------------------------------------------

def test_resolve_config_deep_merges_over_defaults() -> None:
    cfg = resolve_scaffold_config("acme", {"capabilities": {"backend": {"framework": "flask"}}})
    # overridden value wins ...
    assert cfg.option("backend", "framework") == "flask"
    # ... while unspecified defaults survive the merge.
    assert cfg.enabled("backend") is True
    assert cfg.enabled("frontend") is True
    assert cfg.project_name == "acme"
