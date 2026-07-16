"""Unit tests for the deterministic project-scaffold renderer (no LLM, no executor)."""

from app.services.boilerplate import render_scaffold

EXPECTED_PATHS = [
    "Dockerfile",
    ".gitignore",
    "README.md",
    "docker-compose.yml",
    ".env.example",
    "requirements.txt",
    "package.json",
]


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
