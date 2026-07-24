"""Authoritative naming contract distilled from a Design Package (no LLM, deterministic).

The #1 cause of "the generated pieces don't fit together" (a router imports ``UserSchema`` but the
schemas file defines ``UserOut``; a service reads ``email_addr`` but the model field is ``email``;
the frontend api-client calls a path the router never mounts) is that each file is produced in its
OWN isolated LLM call and re-derives identifiers independently. The Design Package ALREADY fixes
these names authoritatively — the DB schema (Mongo ``db_schema.json`` or SQL ``schema.sql``) and the
API mapping. This module distils them into ONE compact block that is prepended to EVERY generation
call's context, so no file can invent a variant.

It is deterministic and returns "" when nothing parses (graceful: no contract == the previous
behavior, so a pack it can't read is never made worse).

Two entry points, one renderer:
* :func:`build_naming_contract` — reads the artifacts from a pack DIRECTORY (used by the
  ``scripts/feature_commit.py`` harness, which loads a pack from disk).
* :func:`build_naming_contract_from_package` — reads them from an in-memory ``design_package``
  dict (artifact-name -> parsed object or raw text), used by the LangGraph code_generator agent,
  which already holds the package in ``WorkflowState`` and never re-reads the disk.

Both feed :func:`_render_contract`, so the emitted block is byte-identical regardless of source.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

_CONTRACT_MAX_CHARS = 6000       # small enough to ride along in every per-file prompt
_CONTRACT_MAX_ENTITIES = 40
_CONTRACT_MAX_ENDPOINTS = 60

# First tokens of a SQL CREATE TABLE body line that are NOT a column definition.
_SQL_NOT_A_COLUMN = {
    "constraint", "primary", "foreign", "unique", "check", "index", "key", "exclude", "like",
}
_MONGO_SCHEMA_NAMES = ("db_schema.json",)
_SQL_SCHEMA_NAMES = ("schema.sql",)
_API_MAPPING_NAMES = ("api-mapping.csv", "api-to-ui-mapping.csv")


def _singularize(name: str) -> str:
    n = name.strip()
    return n[:-1] if len(n) > 1 and n.lower().endswith("s") else n


def _pascal(name: str) -> str:
    parts = [p for p in re.split(r"[_\-\s]+", name.strip()) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or name.strip()


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


def _endpoints_from_csv(text: str) -> list[str]:
    """['METHOD /path -> operation', …] from either api-mapping.csv (http_method/endpoint_path/
    operation_id) or api-to-ui-mapping.csv (combined ``api_endpoint``). Deduped, order preserved."""
    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except (csv.Error, ValueError):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        # DictReader puts extra columns under a None restkey (value is a list) and missing ones as
        # None — coerce defensively so a ragged row can't crash the parse.
        low = {k.strip().lower(): (v.strip() if isinstance(v, str) else "")
               for k, v in row.items() if isinstance(k, str)}
        method, path, op = low.get("http_method", ""), low.get("endpoint_path", ""), low.get("operation_id", "")
        if method and path:
            label = f"{method.upper()} {path}" + (f" -> {op}" if op else "")
        elif low.get("api_endpoint"):
            label = low["api_endpoint"]
        else:
            continue
        if label not in seen:
            seen.add(label)
            out.append(label)
    return out


def _render_contract(entities: list[dict], endpoints: list[str]) -> str:
    """Render the authoritative-identifier block from parsed entities + endpoints (or "" if both
    empty). Shared by both entry points so the emitted contract is source-independent."""
    if not entities and not endpoints:
        return ""
    lines = [
        "NAMING CONTRACT -- use these EXACT identifiers in EVERY file (models, schemas, services, "
        "routers/controllers, and the frontend API client). Do NOT invent, rename, pluralize, or "
        "re-case them; when one file imports or references another file's symbol, the names MUST "
        "match character-for-character.",
    ]
    if entities:
        lines.append("\nEntities -- `ModelClass` <- data store `name`; use these field names verbatim:")
        for e in entities[:_CONTRACT_MAX_ENTITIES]:
            store = f" <- `{e['store']}`" if e["store"] else ""
            fields = ", ".join(e["fields"]) if e["fields"] else "(fields per schema)"
            lines.append(f"- `{e['model']}`{store}: {fields}")
    if endpoints:
        lines.append("\nEndpoints -- keep method, path, and handler name identical across the router, "
                     "service, and the frontend API client:")
        lines.extend(f"- {ep}" for ep in endpoints[:_CONTRACT_MAX_ENDPOINTS])
    block = "\n".join(lines)
    if len(block) > _CONTRACT_MAX_CHARS:
        block = block[:_CONTRACT_MAX_CHARS].rstrip() + "\n- ... (truncated)"
    return block


def build_naming_contract(pack: Path) -> str:
    """Distill the pack's AUTHORITATIVE identifiers (entities + exact field names, and endpoints)
    into one compact must-match block, read from the pack DIRECTORY. Deterministic; returns ""
    when nothing parses (graceful)."""
    def _read(name: str) -> str:
        p = pack / name
        try:
            return p.read_text(encoding="utf-8") if p.exists() else ""
        except OSError:
            return ""

    entities = _entities_from_mongo(next((t for t in map(_read, _MONGO_SCHEMA_NAMES) if t), ""))
    if not entities:
        entities = _entities_from_sql(next((t for t in map(_read, _SQL_SCHEMA_NAMES) if t), ""))
    endpoints: list[str] = []
    for name in _API_MAPPING_NAMES:
        endpoints = _endpoints_from_csv(_read(name))
        if endpoints:
            break
    return _render_contract(entities, endpoints)


def build_naming_contract_from_package(design_package: dict[str, Any] | None) -> str:
    """Same contract, built from an in-memory ``design_package`` dict (artifact-name -> parsed
    object or raw text). Lookup is case-insensitive and coerces parsed JSON back to text so the
    same extractors apply whether the pack arrived parsed (``.json`` packs) or as raw strings."""
    pkg = design_package or {}
    lowered = {k.lower(): v for k, v in pkg.items()}

    def _text(*names: str) -> str:
        for name in names:
            value = pkg[name] if name in pkg else lowered.get(name.lower())
            if value is None:
                continue
            return value if isinstance(value, str) else json.dumps(value)
        return ""

    entities = _entities_from_mongo(_text(*_MONGO_SCHEMA_NAMES))
    if not entities:
        entities = _entities_from_sql(_text(*_SQL_SCHEMA_NAMES))
    endpoints: list[str] = []
    for name in _API_MAPPING_NAMES:
        endpoints = _endpoints_from_csv(_text(name))
        if endpoints:
            break
    return _render_contract(entities, endpoints)


def _prepend_contract(contract: str, ctx: str) -> str:
    """Put the naming contract at the TOP of a layer's design context so every call is bound by it."""
    if not contract:
        return ctx
    return f"{contract}\n\n{ctx}" if ctx else contract
