"""Deterministic, per-application README generator (no LLM).

Sibling of :mod:`app.services.boilerplate`: pure logic, deterministic, no side effects. Where the
old ``README.md.j2`` produced a generic stub (title + run commands), this builds a *real* project
README for the specific application being generated, by mining the Design Package that is already
in hand at scaffold time:

* **project meta** (name / description / version)      ← the resolved capabilities config
* **features** (+ user stories, roles)                 ← ``user_features.json`` / ``user-features.md``
* **tech stack**                                       ← the resolved capabilities config
* **project structure** (the real file tree + specs)   ← ``backend-structure.json`` / ``frontend-structure.json``
* **API reference** (endpoints)                        ← ``openapi.yaml`` / ``openapi.json``
* **screens & routes**                                 ← ``routes.json``
* **data model** (tables / entities)                   ← ``schema.sql`` (or a JSON schema / feature entities)
* **getting started / env / testing**                  ← the resolved capabilities config

Every section is emitted ONLY when its source data is present, so a sparse package (or none at
all) degrades cleanly to the old title + description + run-command README — existing callers and
tests keep working. Output is deterministic: stable ordering, no timestamps, no randomness, which
matches the scaffold-renderer contract.

Artifacts are looked up by their conventional names (case-insensitive, with the aliases real packs
ship), mirroring :func:`app.services.code_generator._artifact`. Values may arrive already parsed
(``.json`` packs) or as raw text (``feature_commit`` loads everything as strings) — ``_coerce``
handles both.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

try:  # PyYAML parses OpenAPI/YAML artifacts; degrade gracefully if it is unavailable.
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

if TYPE_CHECKING:  # avoid a runtime import cycle (boilerplate imports this module)
    from app.services.boilerplate import ScaffoldConfig

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
_STRUCTURE_INDEX_KEYS = ("tree", "structure", "files")
_MAX_SPEC = 100  # per-file description length in the structure tree


# --------------------------------------------------------------------------- artifact access

def _coerce(value: Any) -> Any:
    """Return a parsed object for ``value`` (dict already parsed, or JSON/YAML text), else the raw."""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except ValueError:
            pass
        if yaml is not None:
            try:
                return yaml.safe_load(value)
            except Exception:  # noqa: BLE001 - unparseable text is just "not structured"
                return value
    return value


def _lookup(pkg: dict[str, Any], *names: str) -> Any:
    """First present artifact among ``names`` (case-insensitive). Mirrors code_generator._artifact."""
    lowered = {k.lower(): v for k, v in pkg.items()}
    for name in names:
        if name in pkg:
            return pkg[name]
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _text(pkg: dict[str, Any], *names: str) -> str:
    value = _lookup(pkg, *names)
    return value if isinstance(value, str) else ("" if value is None else json.dumps(value))


# --------------------------------------------------------------------------- extractors

def _features(pkg: dict[str, Any]) -> list[dict[str, str]]:
    """``[{id, name, description}]`` from a ``user_features`` artifact (JSON), else ``[]``."""
    obj = _coerce(_lookup(pkg, "user_features.json", "user-features.json", "user_features"))
    feats = obj.get("features") if isinstance(obj, dict) else None
    out: list[dict[str, str]] = []
    if isinstance(feats, list):
        for feat in feats:
            if not isinstance(feat, dict):
                continue
            name = str(feat.get("name") or feat.get("title") or "").strip()
            if not name:
                continue
            out.append({
                "id": str(feat.get("id") or "").strip(),
                "name": name,
                "description": str(feat.get("description") or feat.get("story") or "").strip(),
            })
    return out


def _roles(pkg: dict[str, Any]) -> list[dict[str, str]]:
    """``[{role, description}]`` from a ``user_features`` artifact, else ``[]``."""
    obj = _coerce(_lookup(pkg, "user_features.json", "user-features.json", "user_features"))
    roles = obj.get("roles") if isinstance(obj, dict) else None
    out: list[dict[str, str]] = []
    if isinstance(roles, list):
        for role in roles:
            if isinstance(role, dict) and role.get("role"):
                out.append({"role": str(role["role"]).strip(), "description": str(role.get("description") or "").strip()})
    return out


def _endpoints(pkg: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``[(METHOD, path, summary)]`` from an OpenAPI spec, in spec order; ``[]`` if none."""
    spec = _coerce(_lookup(pkg, "openapi.yaml", "openapi.yml", "openapi.json", "openapi"))
    paths = spec.get("paths") if isinstance(spec, dict) else None
    out: list[tuple[str, str, str]] = []
    if isinstance(paths, dict):
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, op in methods.items():
                if method.lower() not in _HTTP_METHODS:
                    continue
                summary = ""
                if isinstance(op, dict):
                    summary = str(op.get("summary") or op.get("operationId") or "").strip()
                out.append((method.upper(), str(path), summary))
    return out


def _routes(pkg: dict[str, Any]) -> list[tuple[str, str]]:
    """``[(screen, route)]`` from a ``routes.json`` screen→path map, in map order; ``[]`` if none."""
    obj = _coerce(_lookup(pkg, "routes.json", "routes"))
    if isinstance(obj, dict) and obj and all(isinstance(v, str) for v in obj.values()):
        return [(str(k), v) for k, v in obj.items()]
    return []


def _entities(pkg: dict[str, Any]) -> list[str]:
    """Data-model names: SQL ``CREATE TABLE`` names, else a JSON schema's / feature-pack's entities."""
    sql = _text(pkg, "schema.sql")
    if "create table" in sql.lower():
        tables = re.findall(r"create\s+table\s+(?:if\s+not\s+exists\s+)?[`\"]?(\w+)", sql, re.IGNORECASE)
        if tables:
            return list(dict.fromkeys(tables))
    schema = _coerce(_lookup(pkg, "db_schema.json", "schema.json"))
    if isinstance(schema, dict):
        for key in ("tables", "collections", "entities", "models"):
            block = schema.get(key)
            if isinstance(block, list) and block:
                names = [str(i.get("name") or i.get("table") or i.get("model")) if isinstance(i, dict) else str(i) for i in block]
                names = [n for n in names if n and n != "None"]
                if names:
                    return list(dict.fromkeys(names))
    feats = _coerce(_lookup(pkg, "user_features.json", "user-features.json", "user_features"))
    ents = feats.get("entities") if isinstance(feats, dict) else None
    if isinstance(ents, list) and ents:
        return [str(e) for e in ents if e]
    return []


def _tree_and_notes(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Unwrap a ``*-structure.json`` into ``(file_tree, notes)`` (handles a ``tree`` wrapper)."""
    obj = _coerce(raw)
    if not isinstance(obj, dict):
        return {}, []
    notes = [str(n) for n in obj.get("notes", []) if n] if isinstance(obj.get("notes"), list) else []
    for key in _STRUCTURE_INDEX_KEYS:
        if isinstance(obj.get(key), dict):
            return obj[key], notes
    return {k: v for k, v in obj.items() if k != "notes"}, notes


def _render_tree(tree: dict[str, Any], indent: int = 0) -> list[str]:
    """Indent a structure tree into readable ``dir/`` + ``file — spec`` lines (deterministic order)."""
    lines: list[str] = []
    for key, value in tree.items():
        pad = "  " * indent
        if isinstance(value, dict):
            lines.append(f"{pad}{key}")
            lines.extend(_render_tree(value, indent + 1))
        else:
            spec = " ".join(str(value).split())
            if len(spec) > _MAX_SPEC:
                spec = spec[: _MAX_SPEC - 3].rstrip() + "..."
            lines.append(f"{pad}{key} - {spec}" if spec else f"{pad}{key}")
    return lines


# --------------------------------------------------------------------------- rendering

def render_readme(config: "ScaffoldConfig", design_package: dict[str, Any] | None = None) -> str:
    """Render a comprehensive, application-specific README for the project being generated.

    Deterministic and side-effect free. Sections appear only when the Design Package supplies the
    data behind them, so a sparse/absent package degrades to a title + description + run-commands
    README (backward compatible with the previous ``README.md.j2`` output and its callers/tests).
    """
    pkg = design_package or {}
    name = config.project_name
    description = config.option("project", "description", "") or ""

    blocks: list[str] = [f"# {name}", description] if description else [f"# {name}"]

    stack = _stack_line(config)
    if stack:
        blocks.append(f"> {stack}")

    blocks.append(_features_section(pkg))
    blocks.append(_tech_stack_section(config))
    blocks.append(_structure_section(pkg))
    blocks.append(_getting_started_section(config))
    blocks.append(_api_section(pkg, config))
    blocks.append(_routes_section(pkg, config))
    blocks.append(_data_model_section(pkg, config))
    blocks.append(_testing_section(config))
    blocks.append(_notes_section(pkg))

    body = "\n\n".join(b for b in blocks if b).strip()
    return body + "\n"


def _stack_line(config: "ScaffoldConfig") -> str:
    parts: list[str] = []
    if config.enabled("frontend"):
        fw = str(config.option("frontend", "framework", "react")).capitalize()
        bundler = config.option("frontend", "bundler", "vite")
        parts.append(f"{fw} ({bundler})" if bundler else fw)
    if config.enabled("backend"):
        parts.append(str(config.option("backend", "framework", "fastapi")).capitalize())
    if config.enabled("database"):
        parts.append(str(config.option("database", "provider", "postgres")).capitalize())
    if config.enabled("authentication"):
        parts.append("JWT auth")
    if config.enabled("docker"):
        parts.append("Docker")
    return " | ".join(parts)


def _features_section(pkg: dict[str, Any]) -> str:
    features = _features(pkg)
    roles = _roles(pkg)
    if not features and not roles:
        return ""
    lines = ["## Features"]
    for feat in features:
        label = f"**{feat['name']}**"
        if feat["description"]:
            lines.append(f"- {label}: {feat['description']}")
        else:
            lines.append(f"- {label}")
    if roles:
        lines.append("")
        lines.append("### User roles")
        for role in roles:
            lines.append(f"- **{role['role']}**: {role['description']}" if role["description"] else f"- **{role['role']}**")
    return "\n".join(lines)


def _tech_stack_section(config: "ScaffoldConfig") -> str:
    rows: list[tuple[str, str]] = []
    if config.enabled("frontend"):
        fw = str(config.option("frontend", "framework", "react")).capitalize()
        bundler = config.option("frontend", "bundler", "vite")
        rows.append(("Frontend", f"{fw} + TypeScript" + (f" (bundled with {bundler})" if bundler else "")))
    if config.enabled("backend"):
        rows.append(("Backend", str(config.option("backend", "framework", "fastapi")).capitalize()))
    if config.enabled("database"):
        rows.append(("Database", str(config.option("database", "provider", "postgres")).capitalize()))
    if config.enabled("authentication"):
        rows.append(("Auth", "JWT (access + refresh tokens)"))
    if config.enabled("docker"):
        rows.append(("Container", "Docker + Docker Compose"))
    if not rows:
        return ""
    out = ["## Tech stack", "", "| Layer | Technology |", "| --- | --- |"]
    out += [f"| {layer} | {tech} |" for layer, tech in rows]
    return "\n".join(out)


def _structure_section(pkg: dict[str, Any]) -> str:
    parts: list[str] = []
    for label, aliases in (
        ("Backend", ("backend-structure.json", "backend_structure.json")),
        ("Frontend", ("frontend-structure.json", "frontend_structure.json")),
    ):
        tree, _ = _tree_and_notes(_lookup(pkg, *aliases))
        rendered = _render_tree(tree)
        if rendered:
            parts.append(f"### {label}\n\n```text\n" + "\n".join(rendered) + "\n```")
    if not parts:
        return ""
    return "## Project structure\n\n" + "\n\n".join(parts)


def _getting_started_section(config: "ScaffoldConfig") -> str:
    lines = ["## Getting started", "", "### Prerequisites", ""]
    prereqs: list[str] = []
    if config.enabled("frontend"):
        prereqs.append("- Node.js 18+ and npm")
    if config.enabled("backend"):
        prereqs.append("- Python 3.11+")
    if config.enabled("database"):
        prereqs.append(f"- {str(config.option('database', 'provider', 'postgres')).capitalize()}")
    if config.enabled("docker"):
        prereqs.append("- Docker & Docker Compose (optional)")
    lines += prereqs or ["- None"]

    if config.enabled("backend"):
        framework = config.option("backend", "framework", "fastapi")
        run = "flask --app app.main run --debug" if framework == "flask" else "uvicorn app.main:app --reload"
        lines += ["", "### Backend", "", "```bash", "pip install -r requirements.txt", run, "```"]

    if config.enabled("frontend"):
        scripts = config.option("frontend", "scripts", {}) or {}
        cmds = ["npm install"] + [f"npm run {name}" for name in scripts]
        lines += ["", "### Frontend", "", "```bash", *cmds, "```"]

    if config.enabled("docker"):
        lines += ["", "### With Docker", "", "```bash", "docker compose up --build", "```"]

    variables = config.option("environment", "variables", []) or []
    if config.enabled("environment") and variables:
        lines += ["", "### Environment variables", "",
                  "Copy `.env.example` to `.env` and set:", ""]
        lines += [f"- `{var}`" for var in variables]

    return "\n".join(lines)


def _api_section(pkg: dict[str, Any], config: "ScaffoldConfig") -> str:
    if not config.enabled("backend"):
        return ""
    endpoints = _endpoints(pkg)
    if not endpoints:
        return ""
    out = ["## API reference", "", "| Method | Endpoint | Description |", "| --- | --- | --- |"]
    out += [f"| `{method}` | `{path}` | {summary} |" for method, path, summary in endpoints]
    return "\n".join(out)


def _routes_section(pkg: dict[str, Any], config: "ScaffoldConfig") -> str:
    if not config.enabled("frontend"):
        return ""
    routes = _routes(pkg)
    if not routes:
        return ""
    out = ["## Screens & routes", "", "| Screen | Route |", "| --- | --- |"]
    out += [f"| {screen} | `{route}` |" for screen, route in routes]
    return "\n".join(out)


def _data_model_section(pkg: dict[str, Any], config: "ScaffoldConfig") -> str:
    if not config.enabled("database"):
        return ""
    entities = _entities(pkg)
    if not entities:
        return ""
    return "## Data model\n\n" + "\n".join(f"- `{e}`" for e in entities)


def _testing_section(config: "ScaffoldConfig") -> str:
    if not config.enabled("testing"):
        return ""
    lines = ["## Testing", ""]
    if config.enabled("backend"):
        lines += ["```bash", "pytest", "```"]
    if config.enabled("frontend"):
        scripts = config.option("frontend", "scripts", {}) or {}
        if "test" in scripts:
            lines += ["", "```bash", "npm test", "```"]
    return "\n".join(lines) if len(lines) > 2 else ""


def _notes_section(pkg: dict[str, Any]) -> str:
    notes: list[str] = []
    for aliases in (("backend-structure.json", "backend_structure.json"),
                    ("frontend-structure.json", "frontend_structure.json")):
        _, found = _tree_and_notes(_lookup(pkg, *aliases))
        for note in found:
            if note not in notes:
                notes.append(note)
    if not notes:
        return ""
    return "## Notes\n\n" + "\n".join(f"- {n}" for n in notes)
