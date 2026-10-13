"""Authoritative naming contract distilled from a Design Package (no LLM, deterministic).

The #1 cause of "the generated pieces don't fit together" (a router imports ``UserSchema`` but the
schemas file defines ``UserOut``; a service reads ``email_addr`` but the model field is ``email``;
the frontend api-client calls a path the router never mounts; ``main.py`` imports an
``app.routers`` package nobody generates) is that each file is produced in its OWN isolated LLM
call and re-derives identifiers independently. The Design Package ALREADY fixes these names
authoritatively. This module distils them into compact blocks that ride along with EVERY
generation call, so no file can invent a variant:

* :func:`build_naming_contract_from_package` / :func:`build_naming_contract` — the NAMING CONTRACT:
  the pack-wide canonical values (API base path, stored role strings, error body, env var names,
  helpers defined once — the facts :mod:`app.services.pack_preflight` normalized), every entity
  with its exact field names and allowed values, and every endpoint with its operationId, handler
  name, request/response schema names and who may call it. Built from the shape-based
  :class:`~app.services.pack_index.PackIndex`, so it no longer depends on the artifacts being
  called ``schema.sql`` / ``db_schema.json`` / ``api-mapping.csv`` — the old filename lookup gave
  the current Design-team format an EMPTY entity list.
* :func:`build_module_map` — the MODULE MAP: every source module the PLAN will produce, as the
  import name other files must use. It is built from the run's work items, not the pack, so it
  binds even the files that are generated later than the one importing them.

Both are deterministic and return "" when there is nothing to say (graceful: no contract == the
previous behavior, so a pack it can't read is never made worse). The disk and in-memory entry
points share one renderer, so the block is byte-identical regardless of source.

The legacy text extractors (:func:`_entities_from_sql`, :func:`_entities_from_mongo`,
:func:`_endpoints_from_csv`) are kept with their original return shapes: ``scripts/feature_commit``
re-exports them and the CSV one is still the fallback for packs with no OpenAPI spec.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

_CONTRACT_MAX_CHARS = 24000      # the whole pack's identifiers; shared by every call (prompt-cached)
_CONTRACT_MAX_ENTITIES = 80
_CONTRACT_MAX_ENDPOINTS = 160
_CONTRACT_MAX_ENV_VARS = 80
_MODULE_MAP_MAX_CHARS = 12000

# First tokens of a SQL CREATE TABLE body line that are NOT a column definition.
_SQL_NOT_A_COLUMN = {
    "constraint", "primary", "foreign", "unique", "check", "index", "key", "exclude", "like",
}
_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


# --------------------------------------------------------------------------- naming helpers

def _singularize(name: str) -> str:
    n = name.strip()
    low = n.lower()
    if len(n) > 3 and low.endswith("ies"):
        return n[:-3] + ("Y" if n[-3].isupper() else "y")          # categories -> category
    if len(n) > 4 and low.endswith(("sses", "xes", "ches", "shes", "zzes")):
        return n[:-2]                                               # addresses -> address
    if low.endswith(("ss", "us", "is")):
        return n                                                    # class, status, analysis stay
    return n[:-1] if len(n) > 1 and low.endswith("s") else n


def _pascal(name: str) -> str:
    parts = [p for p in re.split(r"[_\-\s]+", name.strip()) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or name.strip()


def _snake(name: str) -> str:
    """``getDepotDashboard`` -> ``get_depot_dashboard`` (acronyms kept together: ``getOBDData`` ->
    ``get_obd_data``)."""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name.strip())
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return re.sub(r"[^0-9a-zA-Z]+", "_", s).strip("_").lower()


# --------------------------------------------------------------------------- legacy extractors

def _entities_from_mongo(text: str) -> list[dict]:
    """[{model, store, fields[]}] from a Mongo/JSON ``db_schema.json`` (collections[].fields[])."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    for col in data.get("collections", []):
        if not isinstance(col, dict):
            continue
        store = str(col.get("name", "")).strip()
        model = str(col.get("model", "")).strip() or _pascal(_singularize(store))
        fields = [str(f.get("name", "")).strip() for f in col.get("fields", [])
                  if isinstance(f, dict) and str(f.get("name", "")).strip()]
        if model or store:
            out.append({"model": model, "store": store, "fields": fields})
    return out


def _strip_sql_comments(text: str) -> str:
    """Remove ``-- line`` and ``/* block */`` comments so their punctuation (commas especially)
    can't be mistaken for structural SQL. A comma inside a ``-- ...`` comment would otherwise split
    a column definition mid-comment and leak the comment's next word as a fake column."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", text)


def _split_top_level_items(body: str) -> list[str]:
    """Split a ``CREATE TABLE`` body into its column/constraint definitions, on commas ONLY at
    top-level (paren depth 0). A per-LINE split would let a multi-line ``CONSTRAINT ... CHECK (``
    whose condition continues on the next line (e.g. ``CHECK (\\n    price > 0\\n)``) leak that
    continuation line's own identifiers as separate items — one of them then reads like a bare
    column definition and slips a wrong field into the naming contract. Keeping the whole
    parenthesized definition as ONE item, however many lines it spans, fixes that: only its first
    token (``CONSTRAINT``/``CHECK``) is ever checked against ``_SQL_NOT_A_COLUMN``."""
    items: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        items.append("".join(current))
    return items


def _entities_from_sql(text: str) -> list[dict]:
    """[{model, store, fields[]}] from SQL ``CREATE TABLE`` statements (column names, exact case)."""
    text = _strip_sql_comments(text)
    out: list[dict] = []
    for m in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`]?(\w+)[\"`]?\s*\((.*?)\n\s*\)\s*;",
        text, re.IGNORECASE | re.DOTALL,
    ):
        table, body = m.group(1), m.group(2)
        fields: list[str] = []
        for raw in _split_top_level_items(body):
            item = raw.strip().strip(",")
            if not item:
                continue
            first = re.split(r"[\s(]", item, maxsplit=1)[0].strip('"`')
            if first.lower() in _SQL_NOT_A_COLUMN or not re.match(r"^[A-Za-z_]\w*$", first):
                continue
            if first not in fields:
                fields.append(first)
        out.append({"model": _pascal(_singularize(table)), "store": table, "fields": fields})
    return out


def _endpoints_from_rows(rows: Iterable[dict]) -> list[str]:
    """['METHOD /path -> operation', ...] from mapping-table rows: either the legacy shape
    (http_method/endpoint_path/operation_id) or a combined ``api_endpoint`` column. Deduped,
    order preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # DictReader puts extra columns under a None restkey (value is a list) and missing ones as
        # None — coerce defensively so a ragged row can't crash the parse.
        low = {k.strip().lower(): (v.strip() if isinstance(v, str) else "")
               for k, v in row.items() if isinstance(k, str)}
        method, path, op = low.get("http_method", ""), low.get("endpoint_path", ""), low.get("operation_id", "")
        if method and path:
            label = f"{method.upper()} {path}" + (f" -> {op}" if op and op != "-" else "")
        elif low.get("api_endpoint") and re.match(r"^[A-Za-z]+\s+/", low["api_endpoint"]):
            label = low["api_endpoint"]
        else:
            continue
        if label not in seen:
            seen.add(label)
            out.append(label)
    return out


def _endpoints_from_csv(text: str) -> list[str]:
    """:func:`_endpoints_from_rows` over CSV text (``api-mapping.csv`` / ``api-to-ui-mapping.csv``)."""
    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except (csv.Error, ValueError):
        return []
    return _endpoints_from_rows(rows)


# --------------------------------------------------------------------------- pack -> contract parts

def _declared_model_names(index: Any) -> dict[str, str]:
    """``{store: ModelClass}`` where the schema artifact names its model class itself (a Mongo
    ``collections[].model``, an ORM ``class``) — that name beats any derived one."""
    out: dict[str, str] = {}
    for name in index.names("db_schema"):
        obj = index.package.get(name)
        if not isinstance(obj, dict):
            continue
        for key in ("collections", "tables", "entities", "models"):
            for item in obj.get(key) or []:
                if not isinstance(item, dict):
                    continue
                store = str(item.get("name") or item.get("table") or "").strip()
                model = str(item.get("model") or item.get("class") or item.get("class_name") or "").strip()
                if store and model and re.fullmatch(r"[A-Za-z_]\w*", model):
                    out.setdefault(store, model)
    return out


def _entities(index: Any) -> list[dict]:
    """[{model, store, fields[], enums{field: [values]}}] from the normalized tables."""
    models = _declared_model_names(index)
    out = []
    for table in index.tables():
        store = table["name"]
        out.append({
            "model": models.get(store) or _pascal(_singularize(store)),
            "store": store,
            "fields": [c["name"] for c in table["columns"] if c.get("name")],
            "enums": dict(table.get("enums") or {}),
        })
    return out


def _backend_language(index: Any) -> str:
    """``"python"`` / ``"js"`` / ``""`` — what the backend structure tree is written in."""
    hit = index.first("backend_structure")
    if not hit:
        return ""
    from app.services.design_pack import _tree_root

    counts: Counter[str] = Counter()
    stack = [_tree_root(hit[1])]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if isinstance(value, dict):
                stack.append(value)
            elif str(key).endswith(".py"):
                counts["python"] += 1
            elif str(key).endswith((".js", ".ts", ".mjs", ".cjs")):
                counts["js"] += 1
    return counts.most_common(1)[0][0] if counts else ""


def _schema_name(schema: Any) -> str:
    """``LoginRequest`` for a ``$ref``, ``Vehicle[]`` for an array of one, ``""`` otherwise."""
    if not isinstance(schema, dict):
        return ""
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref:
        return ref.rsplit("/", 1)[-1]
    if schema.get("type") == "array":
        inner = _schema_name(schema.get("items"))
        return f"{inner}[]" if inner else ""
    for combo in ("allOf", "oneOf", "anyOf"):
        parts = [_schema_name(s) for s in schema.get(combo) or []]
        if len(parts) == 1 and parts[0]:
            return parts[0]
    return ""


def _json_schema(content: Any) -> Any:
    if not isinstance(content, dict):
        return None
    for media, body in content.items():
        if "json" in str(media).lower() and isinstance(body, dict):
            return body.get("schema")
    return None


def _endpoint_lines(index: Any, facts: dict[str, Any], language: str) -> list[str]:
    """One line per OpenAPI operation; the mapping-table rows when the pack has no spec."""
    spec = index.openapi()
    paths = (spec or {}).get("paths") if isinstance(spec, dict) else None
    if isinstance(paths, dict) and paths:
        op_roles: dict[str, list[str]] = facts.get("operation_roles") or {}
        operations = [
            (method.upper(), str(path), op)
            for path, item in paths.items() if isinstance(item, dict)
            for method, op in item.items() if method.lower() in _HTTP_METHODS and isinstance(op, dict)
        ]
        # Only say "public" when the spec says so: an operation with `security: []`. A spec that
        # never mentions security at all says nothing either way, so neither does the contract.
        declares_security = "security" in spec or any("security" in op for _m, _p, op in operations)
        lines = []
        for method, path, op in operations:
            op_id = str(op.get("operationId") or "").strip()
            label = f"{method} {path}" + (f" -> {op_id}" if op_id else "")
            parts: list[str] = []
            if language == "python" and op_id and _snake(op_id) != op_id:
                parts.append(f"handler {_snake(op_id)}")
            body = _schema_name(_json_schema((op.get("requestBody") or {}).get("content")))
            if body:
                parts.append(f"body {body}")
            for status, response in sorted((op.get("responses") or {}).items(), key=lambda kv: str(kv[0])):
                if not str(status).startswith("2") or not isinstance(response, dict):
                    continue
                returns = _schema_name(_json_schema(response.get("content")))
                shown = returns or ("(no body)" if str(status) == "204" else "")
                if shown:
                    parts.append(f"returns {shown}" if str(status) == "200" else f"returns {status} {shown}")
                break
            security = op["security"] if "security" in op else spec.get("security")
            roles = op_roles.get(f"{method} {path}")
            if roles:
                parts.append("roles: " + ", ".join(roles))
            elif declares_security and security == []:
                parts.append("public")
            elif declares_security and security:
                parts.append("any signed-in user")
            lines.append(label + (" | " + " | ".join(parts) if parts else ""))
        return lines
    rows = index.api_ui_rows()
    if rows:
        found = _endpoints_from_rows(rows)
        if found:
            return found
    for name, value in index.package.items():           # a mapping table nothing classified
        if isinstance(value, str) and str(name).lower().endswith((".csv", ".tsv")):
            found = _endpoints_from_csv(value)
            if found:
                return found
    return []


def _fact_lines(facts: dict[str, Any]) -> list[str]:
    """The canonical values :mod:`app.services.pack_preflight` settled for the whole pack."""
    lines: list[str] = []
    base = facts.get("api_base_path")
    if base:
        lines.append(f"- API base path `{base}`: every router is mounted under it and the frontend API "
                     "client's base URL ends with it. The endpoint paths below are relative to it.")
    roles = facts.get("roles") or {}
    if roles.get("values"):
        line = (f"- Roles: {', '.join(f'`{v}`' for v in roles['values'])} - store, compare and put these "
                "EXACT strings in tokens and role checks")
        labels = [f"{v} = {d}" for v, d in (roles.get("display") or {}).items() if d and d != v]
        if labels:
            line += f"; the pack also writes them as display labels ({', '.join(labels)}), which are UI text ONLY"
        lines.append(line + ".")
    error = facts.get("error_envelope") or {}
    if error.get("schema"):
        required = f" (required: {', '.join(error['required'])})" if error.get("required") else ""
        lines.append(f"- Error responses: every non-2xx body is `{error['schema']}` {{{', '.join(error.get('fields') or [])}}}"
                     f"{required}. The backend returns exactly this shape and the frontend parses exactly this shape.")
    env = list(facts.get("env_vars") or [])
    if env:
        more = f" (+{len(env) - _CONTRACT_MAX_ENV_VARS} more)" if len(env) > _CONTRACT_MAX_ENV_VARS else ""
        lines.append("- Environment variables - read exactly these names, no added prefix, no renames: "
                     + ", ".join(env[:_CONTRACT_MAX_ENV_VARS]) + more + ".")
    auth = facts.get("auth") or {}
    if auth.get("login") and auth.get("user_table"):
        lines.append(f"- Login is `{auth['login']}`, checked against table `{auth['user_table']}`.")
    for symbol, owner in sorted((facts.get("symbol_owners") or {}).items()):
        lines.append(f"- Defined once: `{symbol}` lives in {owner}; every other file imports it from there "
                     "and never redefines it.")
    return lines


def _render_contract(entities: list[dict], endpoints: list[str], fact_lines: list[str] | None = None) -> str:
    """Render the authoritative-identifier block (or "" when there are no entities and no
    endpoints — canonical facts alone are not a naming contract). Shared by both entry points so
    the emitted contract is source-independent."""
    if not entities and not endpoints:
        return ""
    lines = [
        "NAMING CONTRACT -- use these EXACT identifiers in EVERY file (models, schemas, services, "
        "routers/controllers, and the frontend API client). Do NOT invent, rename, pluralize, or "
        "re-case them; when one file imports or references another file's symbol, the names MUST "
        "match character-for-character.",
    ]
    if fact_lines:
        lines.append("\nCanonical values -- where the pack spells one of these differently elsewhere, "
                     "these win:")
        lines.extend(fact_lines)
    if entities:
        lines.append("\nEntities -- `ModelClass` <- data store `name`; use these field names verbatim. A "
                     "field's allowed values follow it in parentheses: store and compare exactly those "
                     "strings (they beat any other spelling in the pack):")
        for e in entities[:_CONTRACT_MAX_ENTITIES]:
            store = f" <- `{e['store']}`" if e["store"] else ""
            enums = e.get("enums") or {}
            fields = ", ".join(
                f"{f} ({'|'.join(enums[f])})" if f in enums else f for f in e["fields"]
            ) if e["fields"] else "(fields per schema)"
            lines.append(f"- `{e['model']}`{store}: {fields}")
        if len(entities) > _CONTRACT_MAX_ENTITIES:
            lines.append(f"- ... (+{len(entities) - _CONTRACT_MAX_ENTITIES} more entities)")
    if endpoints:
        lines.append("\nEndpoints -- `METHOD /path -> operationId`. Keep method, path, and handler name "
                     "identical across the router, service, and the frontend API client: the API client "
                     "function is named by the operationId, the backend handler by `handler` when given "
                     "(else the operationId), and request/response schema classes and frontend types are "
                     "named exactly as `body` / `returns` say:")
        lines.extend(f"- {ep}" for ep in endpoints[:_CONTRACT_MAX_ENDPOINTS])
        if len(endpoints) > _CONTRACT_MAX_ENDPOINTS:
            lines.append(f"- ... (+{len(endpoints) - _CONTRACT_MAX_ENDPOINTS} more endpoints)")
    block = "\n".join(lines)
    if len(block) > _CONTRACT_MAX_CHARS:
        # Cut on a line boundary: half an identifier is worse than none.
        block = block[:_CONTRACT_MAX_CHARS].rsplit("\n", 1)[0] + "\n- ... (truncated)"
    return block


def build_naming_contract_from_package(design_package: dict[str, Any] | None) -> str:
    """The contract for an in-memory ``design_package`` dict (artifact-name -> parsed object or
    raw text) — the shape ``WorkflowState`` and the REST API carry. Artifacts are found by CONTENT,
    whatever they are called. Deterministic; "" when nothing parses."""
    if not design_package:
        return ""
    from app.services.pack_index import index_for
    from app.services.pack_preflight import preflight_for

    index = index_for(design_package)
    facts = preflight_for(design_package).facts
    return _render_contract(
        _entities(index),
        _endpoint_lines(index, facts, _backend_language(index)),
        _fact_lines(facts),
    )


def build_naming_contract(pack: Path) -> str:
    """The same contract, read from a pack DIRECTORY (loaded exactly as a run loads it, so the
    disk and in-memory entry points can never disagree)."""
    from app.services.pack_index import load_pack

    return build_naming_contract_from_package(load_pack(pack))


def _prepend_contract(contract: str, ctx: str) -> str:
    """Put the naming contract at the TOP of a layer's design context so every call is bound by it."""
    if not contract:
        return ctx
    return f"{contract}\n\n{ctx}" if ctx else contract


# --------------------------------------------------------------------------- module map

_SOURCE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".css", ".scss")
#: Directories whose files are run, not imported (migrations, seeds, tests, static files).
_NOT_IMPORTED_DIRS = {"migrations", "alembic", "versions", "seeds", "seeders", "tests", "test", "__tests__",
                      "e2e", "public"}
_TOOL_CONFIG_PREFIXES = ("vite.config", "vitest.config", "jest.config", "jest.setup", "babel.config",
                         "postcss.config", "tailwind.config", "eslint.config", ".eslintrc", "webpack.config",
                         "tsconfig", ".prettierrc", "playwright.config")
_TEST_FILE_RE = re.compile(r"(^test_.*\.py$|_test\.py$|\.(test|spec)\.[cm]?[jt]sx?$)")


def _importable(path: str) -> bool:
    segs = path.split("/")
    base = segs[-1].lower()
    if not base.endswith(_SOURCE_SUFFIXES) or base.startswith(_TOOL_CONFIG_PREFIXES) or _TEST_FILE_RE.search(base):
        return False
    return not any(s.lower() in _NOT_IMPORTED_DIRS for s in segs[:-1])


def _targets(item: Any) -> list[str]:
    files = item.get("target_files") if isinstance(item, dict) else getattr(item, "target_files", None)
    return [str(f).replace("\\", "/").lstrip("/") for f in files or [] if f]


def _python_lines(side: str, paths: list[str]) -> list[str]:
    # The import root is the parent of the SHALLOWEST package. Deeper packages under a folder with
    # no __init__.py (app/modules/auth/ under a bare app/modules/) are namespace packages of it —
    # counting them as roots of their own would rename `app.modules.auth` to `auth`.
    inits = {p.rsplit("/", 1)[0] for p in paths if p.endswith("/__init__.py")}
    top = min(inits, key=lambda d: (d.count("/"), d)) if inits else ""
    root = (top.rsplit("/", 1)[0] + "/" if "/" in top else "") if top else (f"{side}/" if side else "")
    packages: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        if not path.startswith(root):
            continue
        dotted = path[len(root):-3].replace("/", ".")
        if dotted.endswith("__init__"):
            packages.setdefault(dotted[: -len(".__init__")] or "(top level)", [])
            continue
        package, _, module = dotted.rpartition(".")
        packages[package or "(top level)"].append(module)
    if not packages:
        return []
    example = next((p for p in paths if p.startswith(root) and not p.endswith("__init__.py")), "")
    hint = (f", so `{example}` is `{example[len(root):-3].replace('/', '.')}`" if example else "")
    lines = [f"Python modules (import root `{root or './'}`{hint}):"]
    for package in sorted(packages):
        modules = packages[package]
        lines.append(f"- {package}: {', '.join(modules)}" if modules else f"- {package} (package)")
    return lines


def _file_lines(side: str, paths: list[str]) -> list[str]:
    prefix = f"{side}/" if side else ""
    dirs: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        rel = path[len(prefix):] if path.startswith(prefix) else path
        folder, _, name = rel.rpartition("/")
        dirs[folder or "(root)"].append(name)
    if not dirs:
        return []
    lines = [f"Source files (paths relative to `{prefix or './'}`; import them by relative path from the importing file):"]
    lines.extend(f"- {folder}: {', '.join(names)}" for folder, names in dirs.items())
    return lines


def build_module_map(work_items: Iterable[Any] | None, *, for_item: Any = None) -> str:
    """The MODULE MAP for the planned project: every importable source file of the work items, as
    the name other files must import it by (dotted modules for Python, grouped from the real
    import root; folder-grouped paths otherwise).

    ``for_item`` limits it to the top-level folders that item writes into (a backend item gets the
    backend's modules, a frontend item the frontend's), which is the only part it can import.
    "" when there is nothing importable — never a guess.
    """
    by_side: dict[str, list[str]] = defaultdict(list)
    for item in work_items or []:
        for path in _targets(item):
            if _importable(path):
                side = path.split("/", 1)[0] if "/" in path else ""
                if path not in by_side[side]:
                    by_side[side].append(path)
    if for_item is not None:
        wanted = {p.split("/", 1)[0] if "/" in p else "" for p in _targets(for_item)}
        by_side = {s: paths for s, paths in by_side.items() if s in wanted}
    sections: list[str] = []
    for side in sorted(by_side):
        paths = by_side[side]
        py = [p for p in paths if p.endswith(".py")]
        other = [p for p in paths if not p.endswith(".py")]
        block = _python_lines(side, py) + _file_lines(side, other)
        if block:
            sections.append("\n".join(block))
    if not sections:
        return ""
    text = (
        "MODULE MAP -- every source module this project will contain. Import project code ONLY from "
        "these, by exactly these names; some are generated after this file - import them anyway. A "
        "module that is not listed does not exist: never import it, and never invent a package that "
        "is not here.\n\n" + "\n\n".join(sections)
    )
    if len(text) > _MODULE_MAP_MAX_CHARS:
        text = text[:_MODULE_MAP_MAX_CHARS].rsplit("\n", 1)[0] + "\n- ... (truncated)"
    return text
