"""Deterministic project-scaffold renderer (no LLM).

Renders the repo-root boilerplate every generated project needs (Dockerfile, .gitignore,
README.md, docker-compose.yml, .env.example, requirements.txt, package.json) from the
Jinja2 templates in ``app/templates/``, once per run, before any work item is generated —
so the LLM is never asked to produce files whose shape is already known.

Same family as ``plan_builder.py`` / ``manifest_gate.py``: pure logic, no LLM, no side effects
beyond returning content (the caller — ``scaffold_node`` — writes it through the executor).
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

# (template filename, rendered output path) — order is the order files are written/logged.
_SCAFFOLD: list[tuple[str, str]] = [
    ("Dockerfile.j2", "Dockerfile"),
    ("gitignore.j2", ".gitignore"),
    ("README.md.j2", "README.md"),
    ("docker-compose.yml.j2", "docker-compose.yml"),
    ("env.example.j2", ".env.example"),
    ("requirements.txt.j2", "requirements.txt"),
    ("package.json.j2", "package.json"),
]


def render_scaffold(project_id: str) -> list[dict[str, str]]:
    """Render the fixed set of repo-root boilerplate files for ``project_id``.

    Deterministic: same ``project_id`` always renders the same content (no timestamps, no
    randomness), matching the codegen prompt's determinism rule.
    """
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), keep_trailing_newline=True)
    return [
        {"path": out_path, "content": env.get_template(template_name).render(project_id=project_id)}
        for template_name, out_path in _SCAFFOLD
    ]
