"""One loader and one shape-based index over a design package — deterministic, no LLM.

Every consumer used to find artifacts by FILENAME, and the names it knew were one pack format's:
``SKILL.md``, ``schema.sql``, a Mongo ``db_schema.json``. The current Design-team format ships
``skills.md``, ``db_schema.sql`` and a relational ``db_schema.json`` — so a whole format's coding
conventions and database schema silently never reached code generation, on every pack. Five
copies of the pack loader also each parsed only ``.json`` and skipped subfolders.

This module is the single replacement:

* :func:`load_pack` reads a pack directory into the ``name -> content`` dict the pipeline carries
  as ``WorkflowState.design_package`` (JSON parsed, everything else raw text; subfolders included
  under posix-relative keys; the Design pipeline's own state files and binaries skipped). The
  dict shape is unchanged, so the orchestrator API contract is too.
* :class:`PackIndex` classifies every artifact BY CONTENT (``design_pack.classify_value`` — the
  same rules ``detect_roles`` applies on disk) and answers typed questions — the OpenAPI spec, the
  normalized tables with their enum values, the conventions text, the screen's mockup section — no
  matter what the files are called or whether the package came from disk or over the REST API.

Accessors return empty values (``None`` / ``[]`` / ``""``) when a pack lacks an artifact, so a
consumer degrades to its previous behaviour rather than failing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

from app.services import design_pack
from app.services.design_pack import classify_value, is_pipeline_state_file, parse_structured_text

logger = logging.getLogger(__name__)

#: Directories never read as part of a pack.
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".idea", ".vscode"}
#: Single artifacts larger than this are skipped (a pack is text; anything bigger is a dump).
_MAX_FILE_BYTES = 5_000_000


# --------------------------------------------------------------------------- loading

def load_pack(pack_dir: str | Path) -> dict[str, Any]:
    """A pack directory as ``{posix-relative path: content}`` — ``.json`` parsed, everything else
    raw text (YAML included: it is parsed on demand by :class:`PackIndex`, which keeps the dict
    JSON-compatible for checkpoints and the REST API).

    Top-level files keep their bare names exactly as before (``openapi.yaml``); files in
    subfolders are keyed ``er_diagrams/manager.md``. Skipped: binaries (anything that is not
    UTF-8 text — a ``.docx``/``.jpg`` carries no role), files over :data:`_MAX_FILE_BYTES`, VCS/
    tool directories, and the Design pipeline's own bookkeeping (``.*_state.json``,
    ``*.state.json``, ``.signature.json``), which are not design artifacts.
    """
    root = Path(pack_dir)
    package: dict[str, Any] = {}
    if not root.is_dir():
        return package
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in _SKIP_DIRS for part in rel.parts[:-1]) or is_pipeline_state_file(path.name):
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                logger.warning("pack: skipping %s (%d bytes — too large to be a design artifact)", rel, path.stat().st_size)
                continue
            text = path.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, ValueError):
            continue  # binary artifact (.docx, images): no text role
        except OSError:
            continue
        key = rel.as_posix()
        if path.suffix.lower() == ".json":
            try:
                package[key] = json.loads(text)
                continue
            except json.JSONDecodeError:
                pass
        package[key] = text
    return package


# --------------------------------------------------------------------------- schema normalization

_CHECK_IN_RE = re.compile(r"\b(\w+)\s+IN\s*\(([^)]*)\)", re.IGNORECASE)
_SQL_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`]?(?:\w+[\"`]?\.[\"`]?)?(\w+)[\"`]?\s*\((.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_SQL_ALTER_FK_RE = re.compile(
    r"ALTER\s+TABLE\s+[\"`]?(\w+)[\"`]?\s+ADD\s+(?:CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY\s*\(([^)]*)\)\s*"
    r"REFERENCES\s+[\"`]?(\w+)[\"`]?\s*\(([^)]*)\)",
    re.IGNORECASE,
)
_SQL_NOT_A_COLUMN = {"constraint", "primary", "foreign", "unique", "check", "index", "key", "exclude", "like"}


def _enum_values(checks: list[str]) -> dict[str, list[str]]:
    """``{column: [allowed values]}`` from ``col IN ('a', 'b')`` CHECK expressions."""
    enums: dict[str, list[str]] = {}
    for check in checks:
        for col, body in _CHECK_IN_RE.findall(str(check)):
            values = [v.strip().strip("'\"") for v in body.split(",") if v.strip()]
            if values and col not in enums:
                enums[col] = values
    return enums


def _split_sql_items(body: str) -> list[str]:
    items, depth, current = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        items.append("".join(current))
    return items


def _tables_from_sql(text: str, source: str) -> list[dict[str, Any]]:
    text = re.sub(r"--[^\n]*", "", text)
    tables: dict[str, dict[str, Any]] = {}
    for name, body in _SQL_TABLE_RE.findall(text):
        columns, checks, pks, fks = [], [], [], []
        for raw in _split_sql_items(body):
            item = raw.strip()
            if not item:
                continue
            first = re.split(r"[\s(]", item, maxsplit=1)[0].strip('"`').lower()
            if first in _SQL_NOT_A_COLUMN:
                if first == "check":
                    checks.append(re.sub(r"^CHECK\s*", "", item, flags=re.IGNORECASE).strip()[1:-1].strip())
                elif first == "primary":
                    pks.extend(c.strip().strip('"`') for c in re.findall(r"\(([^)]*)\)", item)[0].split(","))
                elif first == "foreign":
                    m = re.search(r"\(([^)]*)\)\s*REFERENCES\s+[\"`]?(\w+)[\"`]?\s*\(([^)]*)\)", item, re.IGNORECASE)
                    if m:
                        fks.append({"columns": [c.strip() for c in m.group(1).split(",")], "ref_table": m.group(2),
                                    "ref_columns": [c.strip() for c in m.group(3).split(",")]})
                continue
            parts = item.split()
            col = parts[0].strip('"`')
            rest = item[len(parts[0]):].strip()
            type_match = re.match(r"([\w\s]+?(?:\([^)]*\))?)(?=\s+(?:NOT|NULL|DEFAULT|PRIMARY|UNIQUE|REFERENCES|CHECK|CONSTRAINT|GENERATED)\b|$)",
                                  rest, re.IGNORECASE)
            default = re.search(r"DEFAULT\s+((?:'[^']*'|\([^)]*\)|[\w.:]+(?:\(\))?))", rest, re.IGNORECASE)
            columns.append({
                "name": col,
                "type": (type_match.group(1).strip() if type_match else rest.split()[0] if rest else "").lower(),
                "nullable": not re.search(r"\bNOT\s+NULL\b|\bPRIMARY\s+KEY\b", rest, re.IGNORECASE),
                "default": default.group(1) if default else None,
            })
            if re.search(r"\bPRIMARY\s+KEY\b", rest, re.IGNORECASE):
                pks.append(col)
            inline_check = re.search(r"\bCHECK\s*\((.*)\)", rest, re.IGNORECASE)
            if inline_check:
                checks.append(inline_check.group(1))
            ref = re.search(r"REFERENCES\s+[\"`]?(\w+)[\"`]?\s*\(([^)]*)\)", rest, re.IGNORECASE)
            if ref:
                fks.append({"columns": [col], "ref_table": ref.group(1), "ref_columns": [ref.group(2).strip()]})
        tables[name] = {"name": name, "columns": columns, "primary_key": pks, "checks": checks,
                        "enums": _enum_values(checks), "foreign_keys": fks, "source": source}
    for table, cols, ref_table, ref_cols in _SQL_ALTER_FK_RE.findall(text):
        if table in tables:
            tables[table]["foreign_keys"].append({
                "columns": [c.strip().strip('"`') for c in cols.split(",")], "ref_table": ref_table,
                "ref_columns": [c.strip().strip('"`') for c in ref_cols.split(",")],
            })
    return list(tables.values())


def _tables_from_json(obj: dict[str, Any], source: str) -> list[dict[str, Any]]:
    """Relational ``tables[]`` (with ``columns[]``) or document ``collections[]`` (with ``fields[]``)."""
    out: list[dict[str, Any]] = []
    for key, field_key in (("tables", "columns"), ("collections", "fields"), ("entities", "fields"), ("models", "fields")):
        block = obj.get(key)
        if not isinstance(block, list):
            continue
        for item in block:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("table") or item.get("model") or "").strip()
            if not name:
                continue
            raw_cols = item.get(field_key) or item.get("columns") or item.get("fields") or []
            columns = [
                {"name": str(c.get("name", "")), "type": str(c.get("type", "")).lower(),
                 "nullable": bool(c.get("nullable", not c.get("required", False))),
                 "default": c.get("default")}
                for c in raw_cols if isinstance(c, dict) and c.get("name")
            ]
            checks = [str(c) for c in item.get("checks") or [] if c]
            fks = [
                {"columns": list(fk.get("columns") or []), "ref_table": fk.get("ref_table") or fk.get("references"),
                 "ref_columns": list(fk.get("ref_columns") or [])}
                for fk in item.get("foreign_keys") or [] if isinstance(fk, dict)
            ]
            enums = _enum_values(checks)
            for c in raw_cols:                 # a column/field may carry its own enum list
                if isinstance(c, dict) and isinstance(c.get("enum"), list) and c.get("name"):
                    enums.setdefault(str(c["name"]), [str(v) for v in c["enum"]])
            primary_key = list(item.get("primary_key") or []) or [
                str(c["name"]) for c in raw_cols
                if isinstance(c, dict) and c.get("name") and (c.get("primaryKey") or c.get("primary_key"))
            ]
            out.append({"name": name, "columns": columns, "primary_key": primary_key,
                        "checks": checks, "enums": enums, "foreign_keys": fks, "source": source})
        if out:
            return out
    return out


# --------------------------------------------------------------------------- the index

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def _tokens(text: str) -> set[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(text))
    return {t for t in re.findall(r"[a-z0-9]+", spaced.lower()) if t not in {"page", "screen", "view"}}


def _has_paths(value: Any) -> bool:
    obj = parse_structured_text(value) if isinstance(value, str) else value
    return isinstance(obj, dict) and isinstance(obj.get("paths"), dict)


#: Fallbacks for a role NO artifact has by shape: an artifact the classifier could not place,
#: whose NAME says what it is and whose content is at least the right kind of thing. Content
#: decides first; this only keeps a sparse or hand-written pack (a one-line ``SKILL.md``, an
#: OpenAPI dict without its ``openapi`` version key) from losing what its file names state.
_NAME_HINTS: tuple[tuple[str, re.Pattern[str], Any], ...] = (
    ("conventions", re.compile(r"skill|guideline|convention|style[-_ ]?guide|coding[-_ ]?standard", re.I),
     lambda v: isinstance(v, str) and bool(v.strip())),
    ("openapi", re.compile(r"openapi|swagger|api[-_ ]?spec", re.I), _has_paths),
    ("validation_rules", re.compile(r"validation", re.I), lambda v: bool(v) and isinstance(v, (dict, str))),
    ("design_tokens", re.compile(r"token", re.I), lambda v: isinstance(v, dict) and bool(v)),
    ("routes", re.compile(r"route", re.I), lambda v: bool(v)),
    ("mockup", re.compile(r"mockup|\.html?$", re.I), lambda v: isinstance(v, str) and "<" in v),
    ("sample_payloads", re.compile(r"payload", re.I), lambda v: isinstance(v, dict) and bool(v)),
)


class PackIndex:
    """Shape-based view over one design package (``name -> parsed JSON or raw text``)."""

    def __init__(self, design_package: dict[str, Any] | None) -> None:
        self.package: dict[str, Any] = dict(design_package or {})
        self._roles: dict[str, list[tuple[str, Any]]] = {}
        # Classification order: top-level files before subfolder ones, then by name — so a
        # top-level artifact wins its role over a copy filed deeper (e.g. a `_handoff/` snapshot).
        for name in sorted(self.package, key=lambda n: (n.count("/"), n.lower())):
            role, obj = classify_value(name, self.package[name])
            if role == "pipeline_state":
                continue
            self._roles.setdefault(role, []).append((name, obj))
        unknown = self._roles.get("unknown", [])
        for role, name_re, fits in _NAME_HINTS:
            if role in self._roles:
                continue
            hit = next(((n, o) for n, o in unknown if name_re.search(Path(n).name) and fits(o)), None)
            if hit:
                self._roles[role] = [hit]
                unknown.remove(hit)
        if "unknown" in self._roles and not unknown:
            del self._roles["unknown"]

    @classmethod
    def from_dir(cls, pack_dir: str | Path) -> PackIndex:
        return cls(load_pack(pack_dir))

    # -- generic -----------------------------------------------------------------

    def roles(self) -> dict[str, list[str]]:
        """``role -> [artifact names]`` (for reporting)."""
        return {role: [name for name, _ in items] for role, items in sorted(self._roles.items())}

    def names(self, role: str) -> list[str]:
        return [name for name, _ in self._roles.get(role, [])]

    def first(self, role: str) -> tuple[str, Any] | None:
        items = self._roles.get(role)
        return items[0] if items else None

    def text(self, name: str) -> str:
        value = self.package.get(name)
        if value is None:
            return ""
        return value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True)

    # -- API ---------------------------------------------------------------------

    def openapi(self) -> dict[str, Any] | None:
        """The OpenAPI spec as a dict, whichever file and format (JSON / YAML) it arrived in."""
        hit = self.first("openapi")
        if hit is None:
            return None
        _name, obj = hit
        if isinstance(obj, dict):
            return obj
        parsed = parse_structured_text(obj) if isinstance(obj, str) else None
        return parsed if isinstance(parsed, dict) else None

    def operations(self) -> list[tuple[str, str, dict[str, Any]]]:
        """``(METHOD, path, operation)`` for every operation of the spec, in spec order."""
        paths = (self.openapi() or {}).get("paths")
        if not isinstance(paths, dict):
            return []
        return [
            (method.upper(), str(path), op)
            for path, item in paths.items() if isinstance(item, dict)
            for method, op in item.items()
            if method.lower() in ("get", "post", "put", "patch", "delete", "head", "options") and isinstance(op, dict)
        ]

    def routes(self) -> Any:
        """The frontend route table as the pack ships it (list / dict / text), or ``None``."""
        hit = self.first("routes")
        return hit[1] if hit else None

    def api_ui_rows(self, screen: str | None = None) -> list[dict[str, str]]:
        """Rows of the UI <-> API mapping table, optionally only those for ``screen``."""
        hit = self.first("api_ui_mapping") or self.first("rich_api_mapping")
        rows = list(hit[1]) if hit and isinstance(hit[1], list) else []
        if screen is None or not rows:
            return rows
        screen_col, _ = design_pack._table_endpoint_columns(list(rows[0].keys()))
        if not screen_col:
            return []
        want = screen.strip().lower()
        return [r for r in rows if str(r.get(screen_col, "")).strip().lower() == want]

    # -- data model --------------------------------------------------------------

    def tables(self) -> list[dict[str, Any]]:
        """Every table/collection, normalized: ``{name, columns[{name,type,nullable,default}],
        primary_key, checks, enums{column: [values]}, foreign_keys[{columns,ref_table,ref_columns}],
        source}``.

        Packs routinely ship the schema twice (``db_schema.json`` AND ``db_schema.sql``). A
        structured source with real columns is preferred; SQL DDL is used otherwise. Enum values
        and foreign keys found only in the other source are merged in, so neither copy's extra
        detail is lost.
        """
        structured: list[dict[str, Any]] = []
        sql: list[dict[str, Any]] = []
        for name, obj in self._roles.get("db_schema", []):
            if isinstance(obj, dict):
                structured.extend(_tables_from_json(obj, name))
            else:
                text = obj if isinstance(obj, str) else self.text(name)
                sql.extend(_tables_from_sql(text, name))
        primary = structured if any(t["columns"] for t in structured) else sql
        secondary = {t["name"]: t for t in (sql if primary is structured else structured)}
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for table in primary:
            if table["name"] in seen:
                continue
            seen.add(table["name"])
            other = secondary.get(table["name"])
            if other:
                for col, values in other["enums"].items():
                    table["enums"].setdefault(col, values)
                if not table["foreign_keys"] and other["foreign_keys"]:
                    table["foreign_keys"] = other["foreign_keys"]
            merged.append(table)
        return merged

    # -- conventions, tokens, rules ----------------------------------------------

    def conventions_text(self) -> str:
        """The coding conventions as text. The richest markdown conventions file wins (packs ship
        a full ``SKILL.md`` next to a short ``skills.md`` excerpt); a JSON-only pack's
        ``categories[].rules`` are rendered as bullets."""
        docs = [(name, obj) for name, obj in self._roles.get("conventions", []) if isinstance(obj, str)]
        if docs:
            return max(docs, key=lambda d: len(d[1]))[1].strip()
        for _name, obj in self._roles.get("conventions", []):
            if isinstance(obj, dict):
                lines: list[str] = []
                for cat in obj.get("categories") or []:
                    if isinstance(cat, dict):
                        lines.append(f"### {cat.get('name', 'Rules')}")
                        lines.extend(f"- {r}" for r in cat.get("rules") or [])
                if not lines:
                    lines.extend(f"- {r}" for r in obj.get("headline_rules") or [])
                return "\n".join(lines).strip()
        return ""

    def tokens(self) -> dict[str, Any] | None:
        hit = self.first("design_tokens")
        return hit[1] if hit and isinstance(hit[1], dict) else None

    def validation_rules(self) -> dict[str, Any] | None:
        hit = self.first("validation_rules")
        return hit[1] if hit and isinstance(hit[1], dict) else None

    def sample_payloads(self) -> dict[str, Any] | None:
        hit = self.first("sample_payloads")
        return hit[1] if hit and isinstance(hit[1], dict) else None

    # -- UI ----------------------------------------------------------------------

    def mockup_html(self) -> str:
        """The richest HTML mockup (packs may ship a functional mockup next to a static one)."""
        docs = [obj for _n, obj in self._roles.get("mockup", []) if isinstance(obj, str)]
        return max(docs, key=len) if docs else ""

    def screen_section(self, screen: str, html: str | None = None) -> str:
        """The mockup ``<section id="screen-...">`` for ``screen`` (whole element), or ``""``.

        Section ids don't always spell the screen name out ("Forgot Password" is
        ``screen-forgot``), so an exact slug match is tried first, then the section whose id words
        best overlap the screen's words.
        """
        html = html if html is not None else self.mockup_html()
        if not html:
            return ""
        sections = list(re.finditer(r"<section\b[^>]*\bid=[\"']screen-([\w-]+)[\"'][^>]*>", html, re.IGNORECASE))
        if not sections:
            return ""
        want_slug, want = _slug(screen), _tokens(screen)
        best, best_score = None, 0.0
        for m in sections:
            sid = m.group(1).lower()
            if sid == want_slug:
                best, best_score = m, 2.0
                break
            have = set(sid.split("-")) - {"page", "screen", "view"}
            if have and want:
                score = len(have & want) / len(have | want)
                if have <= want or want <= have:
                    score += 0.5
                if score > best_score:
                    best, best_score = m, score
        if best is None or best_score < 0.5:
            return ""
        return _balanced_section(html, best.start())

    def state_transitions(self, screen: str | None = None) -> str:
        """The Loading/Empty/Error/Success block for ``screen`` (or the whole document)."""
        hit = self.first("state_transitions")
        doc = hit[1] if hit and isinstance(hit[1], str) else ""
        if not doc or screen is None:
            return doc
        want = _tokens(screen)
        blocks = re.split(r"\n(?=\S[^\n]*:\s*\n|#{1,4}\s)", "\n" + doc)
        for block in blocks:
            head = block.strip().splitlines()[0] if block.strip() else ""
            title = head.lstrip("#").strip().rstrip(":").strip()
            if title and _tokens(title) and (_tokens(title) == want or _slug(title) == _slug(screen)):
                return block.strip()
        return ""

    # -- requirements & environment ---------------------------------------------

    def requirements(self) -> dict[str, list[Any]]:
        """``{functional, non_functional, business_rules, constraints}`` with their TEXT
        (performance requirements are folded into non_functional)."""
        out: dict[str, list[Any]] = {"functional": [], "non_functional": [], "business_rules": [], "constraints": []}
        hit = self.first("requirements")
        obj = hit[1] if hit else None
        if isinstance(obj, list):
            out["functional"] = [r for r in obj if isinstance(r, dict)]
            return out
        if not isinstance(obj, dict):
            return out
        out["functional"] = [r for r in obj.get("functional") or [] if isinstance(r, dict)]
        out["non_functional"] = [r for r in (obj.get("non_functional") or []) + (obj.get("performance") or [])
                                 if isinstance(r, dict)]
        out["business_rules"] = [r for r in obj.get("business_rules") or [] if isinstance(r, dict)]
        out["constraints"] = [c for c in obj.get("constraints") or [] if c]
        return out

    def requirement_text(self, ids: list[str]) -> list[dict[str, Any]]:
        """``[{id, text, ...}]`` for the given requirement ids, in the order asked."""
        by_id = {str(r.get("id")): r for group in self.requirements().values() for r in group if isinstance(r, dict)}
        return [by_id[i] for i in ids if i in by_id]

    def env_vars(self) -> list[dict[str, Any]]:
        """Every environment variable the pack declares: ``{key, example, secret, required,
        comment, group}``, merged from a structured ``env-vars``-style file and ``.env``-style
        text (the structured file wins on metadata; text-only keys are added)."""
        merged: dict[str, dict[str, Any]] = {}
        for _name, obj in self._roles.get("env_vars", []):
            if isinstance(obj, dict) or isinstance(obj, list):
                groups = obj.get("groups") if isinstance(obj, dict) else None
                groups = groups if isinstance(groups, list) else [{"name": "", "vars": obj.get("vars") if isinstance(obj, dict) else obj}]
                for group in groups:
                    for var in (group or {}).get("vars") or []:
                        key = str(var.get("key") or var.get("name") or "").strip()
                        if key and key not in merged:
                            merged[key] = {"key": key, "example": var.get("example", var.get("default", "")),
                                           "secret": bool(var.get("secret", False)),
                                           "required": bool(var.get("required", False)),
                                           "comment": str(var.get("comment", "")), "group": str(group.get("name", ""))}
        for _name, obj in self._roles.get("env_vars", []):
            if isinstance(obj, str):
                comment = ""
                for line in obj.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        comment = stripped.lstrip("#").strip()
                        continue
                    m = re.match(r"(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*)$", stripped)
                    if m and m.group(1) not in merged:
                        merged[m.group(1)] = {"key": m.group(1), "example": m.group(2), "secret": False,
                                              "required": "[required" in comment, "comment": comment, "group": ""}
                    comment = ""
        return list(merged.values())


def _balanced_section(html: str, start: int) -> str:
    """The ``<section>`` element opening at ``start``, through its matching ``</section>``."""
    depth = 0
    for m in re.finditer(r"<(/?)section\b[^>]*>", html[start:], re.IGNORECASE):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return html[start: start + m.end()]
    return html[start:]


# --------------------------------------------------------------------------- per-package cache

_INDEX_CACHE: OrderedDict[str, PackIndex] = OrderedDict()
_INDEX_CACHE_MAX = 8


def _fingerprint(design_package: dict[str, Any]) -> str:
    h = hashlib.sha1()
    for name in sorted(design_package):
        value = design_package[name]
        h.update(name.encode("utf-8", "replace") + b"\0")
        h.update((value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str))
                 .encode("utf-8", "replace") + b"\1")
    return h.hexdigest()


def index_for(design_package: dict[str, Any] | None) -> PackIndex:
    """The :class:`PackIndex` for this package CONTENT, built once and reused.

    Code generation assembles a context for every file of every work item from the same package;
    classifying ~40 artifacts (and parsing a large YAML spec) each time would dominate that. Keyed
    by a content hash, not identity, so an edited re-upload under the same names is re-indexed.
    """
    package = design_package or {}
    key = _fingerprint(package)
    hit = _INDEX_CACHE.get(key)
    if hit is not None:
        _INDEX_CACHE.move_to_end(key)
        return hit
    index = PackIndex(package)
    _INDEX_CACHE[key] = index
    if len(_INDEX_CACHE) > _INDEX_CACHE_MAX:
        _INDEX_CACHE.popitem(last=False)
    return index
