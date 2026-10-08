"""Adaptive design-package resolver.

The design phase hands us a *bundle of artifacts*, but their filenames and formats change
from project to project: the API surface might arrive as ``openapi.yaml`` or a flat
``api-mapping.csv``; the data model as PostgreSQL ``schema.sql`` or a Mongo ``db_schema.json``;
the UI wiring as ``api-mapping.csv`` or ``api-to-ui-mapping.csv``. Hard-coding those names is
brittle — every new hand-off breaks it.

So this module identifies each file by its **role**, inferred from its *content*, not its name:

* ``openapi``            — an OpenAPI/Swagger spec (has ``openapi``/``swagger`` + ``paths``)
* ``api_ui_mapping``     — a table linking UI screens to API endpoints
* ``db_schema``          — SQL DDL *or* a JSON document/relational schema (entities extracted either way)
* ``backend_structure``  — a JSON file-tree that looks like server code
* ``frontend_structure`` — a JSON file-tree that looks like client code
* ``requirements``       — a list of ``{id, text, feature}`` requirements
* ``user_features``      — features with their requirement ids (feature → REQ traceability)
* ``routes``             — a screen-name → URL-path map

:func:`resolve` turns whatever was found into one normalized :class:`ResolvedPack` that the
plan builder consumes, regardless of the source formats. Detection is deterministic
(content sniffing); :func:`llm_classify_unknown` is an optional agentic fallback for files no
detector recognized.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # PyYAML is used for OpenAPI specs; degrade gracefully if unavailable.
    import yaml
except Exception:  # pragma: no cover - yaml is in requirements, this is just belt-and-braces
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _json_compatible_yaml_loader() -> Any:
    """A safe YAML loader (libyaml-backed when available) that leaves dates/timestamps as strings.

    Two reasons. Speed: a real 110 KB OpenAPI spec takes ~1-2 s with the pure-Python loader and a
    fraction of that with ``CSafeLoader``. Shape: a design package must stay JSON-compatible (the
    REST API takes it as JSON, and it rides in every checkpoint), but YAML's implicit timestamp
    rule turns an unquoted ``2024-08-15`` example into a ``datetime.date`` that JSON cannot carry.
    """
    if yaml is None:
        return None
    base = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

    class _JsonCompatLoader(base):  # type: ignore[misc, valid-type]
        pass

    _JsonCompatLoader.yaml_implicit_resolvers = {
        first: [r for r in resolvers if r[0] != "tag:yaml.org,2002:timestamp"]
        for first, resolvers in base.yaml_implicit_resolvers.items()
    }
    return _JsonCompatLoader


_YAML_LOADER = _json_compatible_yaml_loader()


def load_yaml(text: str) -> Any:
    """Parse YAML text with :data:`_YAML_LOADER`; raises on malformed input like ``yaml.load``."""
    if yaml is None or _YAML_LOADER is None:
        raise ValueError("PyYAML is not installed")
    return yaml.load(text, Loader=_YAML_LOADER)  # noqa: S506 - a SafeLoader subclass


#: Parsed JSON/YAML keyed by content hash. A design package is re-indexed for every work item, and
#: parsing a large OpenAPI spec costs real time — once per process per distinct text is enough.
_PARSE_CACHE: OrderedDict[str, Any] = OrderedDict()
_PARSE_CACHE_MAX = 64


def parse_structured_text(text: str, *, yaml_ok: bool = True) -> Any:
    """JSON (when it looks like JSON) or YAML text -> object, cached; ``None`` when unparseable.

    Callers must treat the returned object as read-only: it is shared through the cache.
    """
    digest = hashlib.sha1(f"{int(yaml_ok)}\0{text}".encode("utf-8", "replace")).hexdigest()
    if digest in _PARSE_CACHE:
        _PARSE_CACHE.move_to_end(digest)
        return _PARSE_CACHE[digest]
    try:
        if text.lstrip()[:1] in ("{", "["):
            obj = json.loads(text)
        elif yaml_ok:
            obj = load_yaml(text)
        else:
            obj = None
    except Exception:  # noqa: BLE001 - malformed is just "not structured"
        obj = None
    _PARSE_CACHE[digest] = obj
    if len(_PARSE_CACHE) > _PARSE_CACHE_MAX:
        _PARSE_CACHE.popitem(last=False)
    return obj


#: Files the Design phase's own tooling leaves next to the artifacts: its per-stage state dumps,
#: run signature and orchestrator checkpoint. They are not design artifacts — a stale
#: ``.backend_structure_state.json`` was picked as THE backend structure purely because it sorts
#: before ``backend-structure.json`` — so they are never classified or loaded.
_PIPELINE_STATE_RE = re.compile(r"^(?:\..*state\.json|.+\.state\.json|\.signature\.json)$", re.IGNORECASE)


def is_pipeline_state_file(name: str) -> bool:
    """True for a Design-pipeline bookkeeping file (``.api_contract_state.json``,
    ``api_contract.state.json``, ``.signature.json`` ...) rather than a design artifact."""
    return bool(_PIPELINE_STATE_RE.match(Path(str(name)).name))

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
_STRUCTURE_INDEX_KEYS = ("tree", "structure", "files")


# --------------------------------------------------------------------------- small helpers

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _singular(norm: str) -> str:
    return norm[:-1] if norm.endswith("s") and len(norm) > 1 else norm


def _tokens(text: str) -> set[str]:
    # Split camelCase/PascalCase boundaries BEFORE lowercasing so "LoginPage" tokenizes to
    # {"login", "page"} — not the single glued token {"loginpage"}. Without this, screen-name
    # ↔ page-file matching (plan_builder._match_page uses a token-subset test) never succeeds
    # for PascalCase component files.
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(text))
    return {t for t in re.findall(r"[a-z0-9]+", spaced.lower()) if t}


def endpoint_key(method: str, path: str) -> str:
    """Canonical, param-name-insensitive key: ``POST /a/{}/b`` — used to match across sources."""
    p = re.sub(r"\{[^}]+\}", "{}", path.split("?", 1)[0].strip().rstrip("/")) or "/"
    return f"{method.strip().upper()} {p}"


def _load_structured(path: Path) -> Any:
    """Parse a file as JSON, then YAML; return the object or ``None`` if neither works."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    suffix = path.suffix.lower()
    if suffix in (".json",):
        try:
            return json.loads(text)
        except ValueError:
            return None
    if suffix in (".yaml", ".yml") and yaml is not None:
        try:
            return load_yaml(text)
        except Exception:  # noqa: BLE001 - malformed YAML is just "not structured"
            return None
    # Unknown extension: try JSON then YAML so a mislabeled file is still understood.
    try:
        return json.loads(text)
    except ValueError:
        pass
    if yaml is not None:
        try:
            return load_yaml(text)
        except Exception:  # noqa: BLE001
            return None
    return None


def _rows_from_delimited_text(text: str) -> list[dict[str, str]]:
    """Parse ``text`` as a delimited table (csv/tsv) into row dicts; ``[]`` if it is not clearly
    tabular, has fewer than two real columns, or has no actual DATA rows (header-only).

    Factored out of :func:`_read_table` so :func:`has_rich_api_mapping` — which reads the SAME
    kind of artifact from an in-memory dict rather than a file on disk — parses it with the exact
    same rules rather than a second, independently-maintained copy. The two disagreeing is not
    hypothetical: an earlier version of ``has_rich_api_mapping`` checked only the header row, so a
    header-only CSV (no data rows) registered as a "rich mapping" there while :func:`detect_roles`
    (which calls this same logic) correctly saw no table at all — reproducing, on that one input
    shape, the exact scaffold/plan-builder root disagreement this whole check exists to prevent.
    """
    lines = text.splitlines()
    if not lines:
        return []
    header = lines[0].lstrip()
    if header[:1] in ("{", "["):          # JSON/array, not a table
        return []
    sample = text[:4096]
    if "," not in sample and "\t" not in sample:
        return []
    delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    try:
        reader = csv.DictReader(lines, delimiter=delimiter)
        fieldnames = reader.fieldnames or []
        if len([h for h in fieldnames if h and h.strip()]) < 2:
            return []                      # needs at least two real columns to be a table
        rows = [
            {k: v for k, v in row.items() if k is not None}   # drop csv "extra values" spill
            for row in reader
        ]
    except csv.Error:
        return []
    return [r for r in rows if any((v or "").strip() for v in r.values())]


def _read_table(path: Path) -> list[dict[str, str]]:
    """Read a delimited table (csv/tsv) into row dicts; ``[]`` if it is not clearly tabular."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    return _rows_from_delimited_text(text)


# --------------------------------------------------------------------------- role detection

def _is_openapi(obj: Any) -> bool:
    return isinstance(obj, dict) and ("openapi" in obj or "swagger" in obj) and isinstance(obj.get("paths"), dict)


def _schema_entities(path: Path, obj: Any) -> list[str]:
    """Entity/table names from either SQL DDL or a JSON schema; ``[]`` if it is not a schema."""
    if path.suffix.lower() == ".sql" or (isinstance(obj, str) and "create table" in obj.lower()):
        try:
            sql = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            # A non-UTF-8 dump (cp1252/UTF-16 — common from SQL Server/MySQL on Windows) must
            # degrade to "no entities", not crash resolve(). UnicodeDecodeError is a ValueError,
            # not an OSError, so it must be named explicitly.
            return []
        return re.findall(r"create\s+table\s+(?:if\s+not\s+exists\s+)?[`\"]?(\w+)", sql, re.IGNORECASE)
    if isinstance(obj, dict):
        for key in ("collections", "tables", "entities", "models"):
            block = obj.get(key)
            if isinstance(block, list) and block:
                names: list[str] = []
                for item in block:
                    if isinstance(item, dict):
                        name = item.get("name") or item.get("model") or item.get("table")
                        if name:
                            names.append(str(name))
                    elif isinstance(item, str):
                        names.append(item)
                if names:
                    return names
    return []


def _looks_like_tree(obj: Any) -> bool:
    """A source file-tree: has a ``tree``/``structure`` wrapper OR directory-style keys (``x/``).

    Requiring directory keys (not merely *any* nested dict) keeps config blobs like design
    tokens or validation rules — which are also nested — from being mistaken for a code tree.
    """
    if not isinstance(obj, dict) or not obj:
        return False
    if any(isinstance(obj.get(key), dict) for key in _STRUCTURE_INDEX_KEYS):
        return True
    return any(str(k).endswith("/") for k in obj)


_BACKEND_HINTS = (
    "controller", "service", "mongoose", "express", "middleware", "server.js", "app.js",
    "router", "routes/", "models/", "fastapi", "repository", ".py\"", "schema.sql", "migration",
)
_FRONTEND_HINTS = (
    "pages/", "components/", ".jsx", ".tsx", "react", "hooks/", "store/", ".css", "usestate",
    "component", "vite", "webpack", "redux",
)


def _structure_side(path: Path, raw_text: str) -> str:
    """Classify a file-tree as ``backend_structure`` or ``frontend_structure`` by content hints."""
    name = path.name.lower()
    if "backend" in name or "server" in name:
        return "backend_structure"
    # "ui" as a whole word only — a raw substring test misclassifies backend trees like
    # `build_tree.json` / `guidelines.json` (which merely *contain* "ui") as frontend.
    if "frontend" in name or "client" in name or "web" in name or re.search(r"(?<![a-z])ui(?![a-z])", name):
        return "frontend_structure"
    low = raw_text.lower()
    back = sum(low.count(h) for h in _BACKEND_HINTS)
    front = sum(low.count(h) for h in _FRONTEND_HINTS)
    return "frontend_structure" if front > back else "backend_structure"


def _is_requirements(obj: Any) -> bool:
    block = obj.get("functional") if isinstance(obj, dict) else obj
    if isinstance(block, list) and block:
        first = block[0]
        return isinstance(first, dict) and "id" in first and ("text" in first or "requirement" in first or "description" in first)
    return False


def _is_user_features(obj: Any) -> bool:
    feats = obj.get("features") if isinstance(obj, dict) else None
    if isinstance(feats, list) and feats:
        first = feats[0]
        return isinstance(first, dict) and "requirements" in first
    return False


def _is_routes_map(obj: Any) -> bool:
    if not isinstance(obj, dict) or not obj:
        return False
    vals = [v for v in obj.values() if isinstance(v, str)]
    return len(vals) == len(obj) and sum(1 for v in vals if v.startswith("/")) >= max(3, len(vals) // 2)


# --- roles for the artifacts that only ever shape the GENERATED CODE (conventions, tokens, rules,
# mockup ...). Before these existed every such file was "unknown", so nothing downstream could
# find it except by guessing its filename — and the guesses were the previous pack format's names
# (SKILL.md, schema.sql), which is how a whole pack format's conventions and schema went missing.

_RULE_KEYS = {"required", "message", "pattern", "minlength", "maxlength", "min", "max", "enum", "format"}
_TOKEN_VALUE_RE = re.compile(
    r"^\s*(?:#[0-9a-fA-F]{3,8}|rgba?\(.+\)|hsla?\(.+\)|-?\d+(?:\.\d+)?(?:px|rem|em|%|ms|s|vh|vw)?"
    r"|\d+(?:\.\d+)?px\s*/\s*\d+(?:\.\d+)?px|0 \d.*|[\"']?[A-Z][\w -]+[\"']?\s*,.*)\s*$"
)


def _dict_children(obj: Any) -> list[dict[str, Any]]:
    return [v for v in obj.values() if isinstance(v, dict)] if isinstance(obj, dict) else []


def _is_rule_dict(obj: Any) -> bool:
    return isinstance(obj, dict) and bool({str(k).lower() for k in obj} & _RULE_KEYS)


def _is_validation_rules(obj: Any) -> bool:
    """``{group: {field: {required, message, ...}}}`` (or the flat ``{field: {...}}``) — with at
    least one human-facing ``message``, which is what the generated code must copy verbatim."""
    if not isinstance(obj, dict) or _is_openapi(obj):
        return False
    for level in (_dict_children(obj), [f for g in _dict_children(obj) for f in _dict_children(g)]):
        if len(level) >= 2:
            rules = [d for d in level if _is_rule_dict(d)]
            if len(rules) >= 2 and len(rules) >= 0.6 * len(level) and any("message" in d for d in rules):
                return True
    return False


def _leaf_strings(obj: Any, limit: int = 2000) -> list[str]:
    out: list[str] = []
    stack = [obj]
    while stack and len(out) < limit:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str):
            out.append(node)
    return out


def _is_design_tokens(obj: Any) -> bool:
    """Nested name -> colour / size / font values (flat maps or ``{"value": ...}`` token objects)."""
    if not isinstance(obj, dict):
        return False
    leaves = _leaf_strings(obj)
    hits = sum(1 for leaf in leaves if _TOKEN_VALUE_RE.match(leaf))
    return hits >= 8 and hits >= 0.4 * len(leaves)


def _is_sample_payloads(obj: Any) -> bool:
    """``{operation: {method, path, request?, responses?}}`` — example traffic per operation."""
    kids = _dict_children(obj)
    hits = [k for k in kids if "method" in k and "path" in k and ("responses" in k or "request" in k)]
    return len(hits) >= 2 and len(hits) >= 0.6 * len(kids)


def _is_env_vars(obj: Any) -> bool:
    groups = obj.get("groups") if isinstance(obj, dict) else None
    if isinstance(groups, list) and groups and isinstance(groups[0], dict):
        return isinstance(groups[0].get("vars"), list)
    items = obj.get("vars") if isinstance(obj, dict) else obj
    return (
        isinstance(items, list) and len(items) >= 3
        and all(isinstance(i, dict) and ("key" in i or "name" in i) for i in items[:5])
        and any(("example" in i or "default" in i or "value" in i) for i in items[:5])
    )


def _is_conventions_json(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    cats = obj.get("categories")
    if isinstance(cats, list) and cats and isinstance(cats[0], dict) and "rules" in cats[0]:
        return True
    return isinstance(obj.get("headline_rules"), list) and bool(obj["headline_rules"])


def _is_glossary(obj: Any) -> bool:
    terms = obj.get("terms") if isinstance(obj, dict) else None
    return isinstance(terms, list) and bool(terms) and isinstance(terms[0], dict) and "term" in terms[0]


def _is_decisions_json(obj: Any) -> bool:
    return isinstance(obj, dict) and any(k in obj for k in ("srs_concerns", "decisions", "open_questions"))


def _structured_extra_role(obj: Any) -> str:
    """Role for a parsed JSON/YAML artifact the planning detectors don't use, or ``""``."""
    for role, test in (
        ("validation_rules", _is_validation_rules),
        ("sample_payloads", _is_sample_payloads),
        ("env_vars", _is_env_vars),
        ("conventions", _is_conventions_json),
        ("glossary", _is_glossary),
        ("decisions", _is_decisions_json),
        ("design_tokens", _is_design_tokens),
    ):
        if test(obj):
            return role
    return ""


#: Extensions read as documents (markdown, HTML, plain text, env files) — never YAML-parsed:
#: YAML happily turns a markdown checklist into a list and a ``# heading`` into a comment, which
#: made every such file "structured but unknown".
_TEXT_DOC_SUFFIXES = {".md", ".markdown", ".html", ".htm", ".txt", ".mmd", ".mermaid", ".example", ".env"}
_MERMAID_RE = re.compile(
    r"^\s*(?:```\s*mermaid\s*\n\s*)?(?:erDiagram|flowchart\b|graph\s+(?:TD|TB|BT|RL|LR)\b|sequenceDiagram|"
    r"classDiagram|stateDiagram(?:-v2)?|journey|gantt|mindmap)",
)
_STATE_LINE_RE = re.compile(r"^\s*[-*]\s*(?:Loading|Empty|Error|Success)\b\s*(?:→|->|:|—|–)", re.IGNORECASE | re.MULTILINE)
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\]", re.MULTILINE)
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?[A-Z][A-Z0-9_]*\s*=", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+)$", re.MULTILINE)
_RULE_WORDS_RE = re.compile(r"\b(?:must|never|always|do not|don't|prefer|avoid|should|only)\b", re.IGNORECASE)
_REQ_BULLET_RE = re.compile(r"^\**\[?[A-Z]{2,5}-\d+")
_CONVENTIONS_NAME_RE = re.compile(r"skill|guideline|convention|style[-_ ]?guide|coding[-_ ]?standard", re.IGNORECASE)


def _text_role(name: str, text: str) -> str:
    """Role for a document-shaped artifact (markdown / HTML / env), or ``""``."""
    head = text[:8000]
    low_name = Path(name).name.lower()
    if re.search(r"<(?:!doctype html|html|body)\b", head, re.IGNORECASE) or (
        low_name.endswith((".html", ".htm")) and re.search(r"<(?:section|div|main)\b", head, re.IGNORECASE)
    ):
        return "mockup"
    if _MERMAID_RE.match(text):
        return "diagrams"
    content_lines = [ln for ln in head.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    env_lines = _ENV_LINE_RE.findall(head)
    if len(env_lines) >= 3 and len(env_lines) >= 0.6 * len(content_lines):
        return "env_vars"   # mostly KEY=value lines: a .env.example, not a doc quoting a few
    if len(_STATE_LINE_RE.findall(text)) >= 3:
        return "state_transitions"
    if re.search(r"^#{1,3}\s*(?:Chosen\b|.*Design Decision)", head, re.MULTILINE):
        return "decisions"
    if re.match(r"^\s*#\s*Glossary\b", head, re.IGNORECASE):
        return "glossary"
    bullets = _BULLET_RE.findall(text)
    rule_bullets = [b for b in bullets if _RULE_WORDS_RE.search(b)]
    req_bullets = [b for b in bullets if _REQ_BULLET_RE.match(b.strip())]
    if _CONVENTIONS_NAME_RE.search(low_name) and (rule_bullets or bullets):
        return "conventions"
    if len(rule_bullets) >= 8 and len(req_bullets) < 3:
        return "conventions"
    if len(_CHECKBOX_RE.findall(text)) >= 5:
        return "manifest_index"
    return ""


def _table_endpoint_columns(headers: list[str]) -> tuple[str | None, str | None]:
    """Locate the (screen, endpoint) columns in a UI↔API table by fuzzy header names."""
    low = {h.lower(): h for h in headers if h}
    screen = next((low[h] for h in low if "screen" in h or h in ("page", "view", "ui")), None)
    endpoint = next((low[h] for h in low if "endpoint" in h or h in ("api", "api_endpoint", "route")), None)
    return screen, endpoint


def _is_rich_api_mapping(headers: list[str]) -> bool:
    """The legacy flat mapping: has its own operation_id + req_ids + endpoint_path columns."""
    hs = {h.lower() for h in headers}
    return {"operation_id", "endpoint_path"}.issubset(hs) and any("req" in h for h in hs)


def has_rich_api_mapping(design_package: dict[str, Any]) -> bool:
    """Dict-based equivalent of ``detect_roles(pack_dir).get("rich_api_mapping")``.

    ``app.services.boilerplate`` never receives ``pack_dir`` — only the already-parsed artifact
    dict (``state["design_package"]``) — but it needs the SAME signal ``build_plan`` uses to pick
    its builder: the legacy flat-CSV path keeps every path byte-for-byte, while the adaptive
    (structure-tree) path unconditionally re-roots every generated file under canonical
    ``backend/``/``frontend/`` folders (see ``plan_builder._reroot``), discarding whatever wrapper
    directory name the structure trees actually used. ``boilerplate.py``'s OWN root computation
    (``_single_top_dir``) has no way to know about that rerooting, so left alone the two modules
    can disagree on where a project's files live — which is exactly how a scaffolded
    ``jest.config``'s ``rootDir`` and ``@/`` alias ended up pointing at a path nothing was ever
    generated under. This function lets ``boilerplate.py`` ask the same "will build_plan reroot
    this?" question ``detect_roles`` answers from disk, from the in-memory dict it actually has.

    Reads every CSV/TSV-shaped artifact (raw text, as ``app.services.plan_builder._load_pack_artifacts``
    stores it, OR an already-parsed list of row dicts) via the SAME :func:`_rows_from_delimited_text`
    logic ``_read_table``/``detect_roles`` use — not a second, hand-rolled parse — then applies the
    identical header check :func:`_is_rich_api_mapping` uses against the first actual DATA row.

    Requiring a real data row (not just a header) matters: ``detect_roles`` only ever sets the
    ``rich_api_mapping`` role when ``_read_table`` returns at least one row, so a header-only CSV
    is invisible to it — ``build_plan()`` would take the adaptive (re-rooted) path for such a pack.
    Checking headers alone here would answer the opposite way for the exact same file, silently
    reintroducing the scaffold/plan-builder root mismatch this function exists to prevent.
    """
    for name, value in design_package.items():
        if not str(name).lower().endswith((".csv", ".tsv")):
            continue
        if isinstance(value, list) and value and isinstance(value[0], dict):
            rows = [r for r in value if any((v or "").strip() for v in r.values())]
        elif isinstance(value, str):
            rows = _rows_from_delimited_text(value)
        else:
            continue
        if rows and _is_rich_api_mapping(list(rows[0].keys())):
            return True
    return False


@dataclass
class DetectedFile:
    path: Path
    role: str
    obj: Any = None


def detect_roles(pack_dir: str | Path) -> dict[str, list[DetectedFile]]:
    """Classify every top-level file in ``pack_dir`` by role, from content. Names are irrelevant."""
    pack = Path(pack_dir)
    roles: dict[str, list[DetectedFile]] = defaultdict(list)
    if not pack.is_dir():
        return roles

    def _record_table(path: Path) -> bool:
        rows = _read_table(path)
        if not rows:
            return False
        screen_col, endpoint_col = _table_endpoint_columns(list(rows[0].keys()))
        if not (endpoint_col or screen_col):
            return False
        role = "rich_api_mapping" if _is_rich_api_mapping(list(rows[0].keys())) else "api_ui_mapping"
        roles[role].append(DetectedFile(path, role, rows))
        return True

    for path in sorted(pack.iterdir()):
        if not path.is_file() or is_pipeline_state_file(path.name):
            continue
        suffix = path.suffix.lower()

        # 1. Delimited tables (csv/tsv) → UI↔API mapping.
        if suffix in (".csv", ".tsv") and _record_table(path):
            continue

        # 2. SQL schema.
        if suffix == ".sql":
            roles["db_schema"].append(DetectedFile(path, "db_schema", None))
            continue

        # 3. Documents (markdown / HTML / env files): classified as text, never YAML-parsed.
        if suffix in _TEXT_DOC_SUFFIXES or path.name.lower().startswith(".env"):
            try:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                text = ""
            if suffix == ".txt" and "create table" in text.lower():
                roles["db_schema"].append(DetectedFile(path, "db_schema", None))  # a SQL dump saved as .txt
                continue
            role = _text_role(path.name, text)
            if role:
                roles[role].append(DetectedFile(path, role, None))
            elif not _record_table(path):
                roles.setdefault("unknown", []).append(DetectedFile(path, "unknown", None))
            continue

        # 4. Structured (JSON/YAML) files — parsed FIRST so JSON isn't mistaken for CSV.
        obj = _load_structured(path)
        if obj is not None and not isinstance(obj, str):
            role, obj = _structured_role(path, obj)
            roles[role].append(DetectedFile(path, role, obj))
            continue
        if isinstance(obj, str) and "create table" in obj.lower():
            roles["db_schema"].append(DetectedFile(path, "db_schema", None))  # SQL with an odd extension
            continue

        # 5. Fallback: a table with a non-standard extension (e.g. no extension).
        if _record_table(path):
            continue

        roles.setdefault("unknown", []).append(DetectedFile(path, "unknown", None))

    return roles


def _structured_role(
    path: Path, obj: Any, raw_text: str | None = None, *, from_disk: bool = True,
) -> tuple[str, Any]:
    """Role of a parsed JSON/YAML artifact — ONE ordered chain shared by :func:`detect_roles`
    (files on disk) and :func:`classify_value` (an in-memory design package, ``from_disk=False``:
    a relative artifact name must never be resolved against the process's working directory)."""
    if _is_openapi(obj):
        return "openapi", obj
    if _is_user_features(obj):
        return "user_features", obj
    if _is_requirements(obj):
        return "requirements", obj
    if _schema_entities(path, obj):
        return "db_schema", obj
    if _is_routes_map(obj):
        return "routes", obj
    if _looks_like_tree(obj):
        if raw_text is None and from_disk:
            try:
                raw_text = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                raw_text = None
        return _structure_side(path, raw_text if raw_text is not None else json.dumps(obj)), obj
    return (_structured_extra_role(obj) or "unknown"), obj


def classify_value(name: str, value: Any) -> tuple[str, Any]:
    """Role of ONE artifact held in memory (``name -> parsed JSON or raw text``), with the object
    the role's consumers expect: table rows for a mapping, the parsed object for JSON/YAML, the
    text itself for documents and SQL.

    The in-memory twin of :func:`detect_roles`, so a design package that arrived over the REST API
    or sits in ``WorkflowState`` is classified by exactly the rules a pack directory is. Never
    raises: anything unrecognisable is ``("unknown", value)``.
    """
    path = Path(str(name))
    suffix = path.suffix.lower()
    if is_pipeline_state_file(path.name):
        return "pipeline_state", value
    text = value if isinstance(value, str) else None

    def _table() -> tuple[str, Any] | None:
        rows = _rows_from_delimited_text(text) if text is not None else (
            value if isinstance(value, list) and value and isinstance(value[0], dict) else []
        )
        if not rows:
            return None
        screen_col, endpoint_col = _table_endpoint_columns(list(rows[0].keys()))
        if not (endpoint_col or screen_col):
            return None
        return ("rich_api_mapping" if _is_rich_api_mapping(list(rows[0].keys())) else "api_ui_mapping"), rows

    if suffix in (".csv", ".tsv"):
        table = _table()
        if table:
            return table
    if suffix == ".sql":
        return "db_schema", text
    if text is not None and (suffix in _TEXT_DOC_SUFFIXES or path.name.lower().startswith(".env")):
        if suffix == ".txt" and "create table" in text.lower():
            return "db_schema", text            # a SQL dump saved as .txt (same rule as detect_roles)
        role = _text_role(path.name, text)
        if role:
            return role, text
        return _table() or ("unknown", text)

    obj: Any = None if text is not None else value
    if text is not None:
        stripped = text.lstrip()
        if suffix == ".json" or stripped[:1] in ("{", "["):
            obj = parse_structured_text(text, yaml_ok=False)
        elif suffix in (".yaml", ".yml") or re.match(r"(openapi|swagger)\s*:", stripped):
            obj = parse_structured_text(text)
    if obj is not None and not isinstance(obj, str):
        return _structured_role(path, obj, raw_text=text, from_disk=False)
    if text is not None:
        if "create table" in text[:50_000].lower():
            return "db_schema", text
        table = _table()
        if table:
            return table
        role = _text_role(path.name, text)
        if role:
            return role, text
    return "unknown", value


def _first(roles: dict[str, list[DetectedFile]], role: str) -> DetectedFile | None:
    items = roles.get(role)
    return items[0] if items else None


def is_design_pack(pack_dir: str | Path) -> bool:
    """True if the folder holds something we can decompose (an API surface, in any form)."""
    roles = detect_roles(pack_dir)
    return any(roles.get(r) for r in ("openapi", "api_ui_mapping", "rich_api_mapping"))


# --------------------------------------------------------------------------- normalization

@dataclass
class ResolvedPack:
    """Format-independent view of a design package."""

    endpoints: list[dict[str, Any]] = field(default_factory=list)   # {method, path, operation_id, tag}
    entities: list[str] = field(default_factory=list)               # table / collection names
    screens: list[dict[str, Any]] = field(default_factory=list)     # {name, route, endpoints:[key]}
    backend_tree: dict[str, Any] = field(default_factory=dict)
    frontend_tree: dict[str, Any] = field(default_factory=dict)
    tag_reqs: dict[str, list[str]] = field(default_factory=dict)    # openapi tag → [REQ ids]
    roles: dict[str, list[str]] = field(default_factory=dict)       # role → [filename] (for reporting)

    def endpoint_by_key(self) -> dict[str, dict[str, Any]]:
        return {endpoint_key(e["method"], e["path"]): e for e in self.endpoints}


def _tree_root(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        for key in _STRUCTURE_INDEX_KEYS:
            if isinstance(obj.get(key), dict):
                return obj[key]
        return obj
    return {}


def _endpoints_from_openapi(spec: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            tags = op.get("tags") or []
            out.append({
                "method": method.upper(),
                "path": path,
                "operation_id": op.get("operationId") or "",
                "tag": (tags[0] if tags else "") or "",
            })
    return out


def _parse_endpoint_cell(cell: str) -> tuple[str, str] | None:
    """'POST /auth/login' → ('POST', '/auth/login'); tolerant of stray whitespace."""
    parts = str(cell).split()
    if len(parts) >= 2 and parts[0].upper() in {m.upper() for m in _HTTP_METHODS}:
        return parts[0].upper(), parts[1]
    if len(parts) == 1 and parts[0].startswith("/"):
        return "GET", parts[0]
    return None


def _endpoints_from_mapping(rows: list[dict[str, str]], screen_col: str, endpoint_col: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        parsed = _parse_endpoint_cell(row.get(endpoint_col, ""))
        if not parsed:
            continue
        method, path = parsed
        key = endpoint_key(method, path)
        if key in seen:
            continue
        seen.add(key)
        seg = re.sub(r"\{[^}]+\}", "", path).strip("/").split("/")
        out.append({"method": method, "path": path, "operation_id": "", "tag": seg[0] if seg and seg[0] else ""})
    return out


def _screens_from_mapping(
    rows: list[dict[str, str]], screen_col: str, endpoint_col: str, routes: dict[str, str],
) -> list[dict[str, Any]]:
    routes_norm = {_norm(k): v for k, v in routes.items()}
    grouped: dict[str, list[str]] = defaultdict(list)
    order: list[str] = []
    for row in rows:
        name = (row.get(screen_col) or "").strip()
        if not name:
            continue
        if name not in grouped:
            order.append(name)
        parsed = _parse_endpoint_cell(row.get(endpoint_col, ""))
        if parsed:
            key = endpoint_key(*parsed)
            if key not in grouped[name]:
                grouped[name].append(key)
        else:
            grouped[name]  # ensure the screen is registered even with no endpoint
    screens = []
    for name in order:
        screens.append({
            "name": name,
            "route": routes.get(name) or routes_norm.get(_norm(name), ""),
            "endpoints": grouped[name],
        })
    return screens


def _build_tag_reqs(roles: dict[str, list[DetectedFile]], tags: list[str]) -> dict[str, list[str]]:
    """Map each OpenAPI tag to requirement ids via the feature whose name it matches.

    Matching is **token-first, substring-as-fallback**, and the order matters. The original
    implementation only did ``stem in norm_feature_name`` against a name with every non-alphanumeric
    stripped out ("Orders & Fulfilment" -> "ordersfulfilment"), which over-matches: any tag whose
    singular stem happens to appear anywhere inside a glued feature name collects that feature's
    ENTIRE requirement list, and a tag matching two feature names silently collects the union of
    both. Tag ``order`` would take the requirements of a "Reorder" feature; tag ``cat`` would take
    "Catalogue & Browsing".

    So: first try an exact match against the feature name's individual words (both sides
    singularised), which is what actually expresses "this tag is that feature". Only if no feature
    matches that way does the old substring test run, so a pack that resolves correctly today
    resolves identically — this is a precision fix, not a behaviour change for the working case.

    Tags that resolve to nothing are logged. They are not an error (a tag may genuinely have no
    requirements behind it), but silence is how the resulting empty ``requirement_ids`` on a work
    item goes unnoticed, and traceability is the thing this function exists to provide.

    Granularity is a known limit either way: a tag inherits its whole feature's requirements, so
    ``cart`` and ``checkout`` both map to all of "Cart & Checkout". Splitting a feature's
    requirements across its tags needs per-requirement tagging that the design pack does not carry.
    """
    # (glued name, singularised word tokens, reqs) — the tokens are what makes a precise match
    # possible, and they are lost by _norm, so both forms are kept.
    features: list[tuple[str, set[str], list[str]]] = []

    def _add(name: str, reqs: list[str]) -> None:
        if name and reqs:
            features.append((_norm(name), {_singular(t) for t in _tokens(name)}, reqs))

    uf = _first(roles, "user_features")
    if uf and isinstance(uf.obj, dict):
        for feat in uf.obj.get("features", []):
            if isinstance(feat, dict) and feat.get("name"):
                _add(str(feat["name"]), [str(r) for r in feat.get("requirements", []) if r])
    if not features:  # fall back to requirements grouped by their 'feature' field
        req = _first(roles, "requirements")
        block = (req.obj.get("functional") if req and isinstance(req.obj, dict) else req.obj) if req else None
        if isinstance(block, list):
            grouped: dict[str, list[str]] = defaultdict(list)
            for item in block:
                if isinstance(item, dict) and item.get("feature") and item.get("id"):
                    grouped[str(item["feature"])].append(str(item["id"]))
            for name, reqs in grouped.items():
                _add(name, reqs)

    tag_reqs: dict[str, list[str]] = {}
    unmapped: list[str] = []
    ambiguous: list[str] = []
    for tag in tags:
        if not tag:
            continue
        stem = _singular(_norm(tag))
        if not stem:
            continue

        matched = [reqs for _, toks, reqs in features if stem in toks]
        if not matched:
            # No feature names this tag as one of its words. Fall back to the looser test, because
            # abbreviated tags are normal and real: "auth" is not a word of "Account &
            # Authentication", but it plainly means it, and the live pack depends on that mapping.
            #
            # The fallback accepts a match only when it is UNAMBIGUOUS. Taking the union of several
            # loose matches is the actual damage the old substring test did: a tag would silently
            # inherit two unrelated features' requirements, and nothing downstream could tell. An
            # ambiguous tag is reported instead, because a wrong mapping is worse than a missing one.
            loose = [(gname, reqs) for gname, _, reqs in features if stem in gname]
            if len(loose) == 1:
                matched = [loose[0][1]]
            elif len(loose) > 1:
                ambiguous.append(f"{tag} (matches {', '.join(sorted(g for g, _ in loose))})")

        if matched:
            tag_reqs[tag] = sorted(dict.fromkeys(r for reqs in matched for r in reqs))
        else:
            unmapped.append(tag)

    if ambiguous:
        logger.warning(
            "design pack: %d OpenAPI tag(s) match more than one feature by name and were left "
            "unmapped rather than given the union of both: %s. Rename the tag to one of the "
            "feature's own words, or add the tag to that feature, to resolve it.",
            len(ambiguous), "; ".join(sorted(ambiguous)),
        )
    if unmapped:
        logger.warning(
            "design pack: %d OpenAPI tag(s) map to no requirement ids: %s. Work items for those "
            "endpoints will carry requirement_ids=[] - traceability for them is unavailable, not "
            "empty by design.",
            len(unmapped), ", ".join(sorted(unmapped)),
        )
    return tag_reqs


def resolve(pack_dir: str | Path, roles: dict[str, list[DetectedFile]] | None = None) -> ResolvedPack | None:
    """Normalize a design package into a :class:`ResolvedPack`, or ``None`` if unrecognizable."""
    pack = Path(pack_dir)
    roles = roles if roles is not None else detect_roles(pack)

    # Endpoints: prefer the authoritative OpenAPI spec; fall back to the UI↔API mapping.
    endpoints: list[dict[str, Any]] = []
    openapi = _first(roles, "openapi")
    mapping = _first(roles, "api_ui_mapping") or _first(roles, "rich_api_mapping")
    screen_col = endpoint_col = None
    if mapping:
        screen_col, endpoint_col = _table_endpoint_columns(list(mapping.obj[0].keys()))
    if openapi and isinstance(openapi.obj, dict):
        endpoints = _endpoints_from_openapi(openapi.obj)
    elif mapping and endpoint_col:
        endpoints = _endpoints_from_mapping(mapping.obj, screen_col or "", endpoint_col)

    if not endpoints and not (mapping and screen_col):
        return None

    # Entities from whichever schema showed up (SQL or JSON).
    entities: list[str] = []
    schema = _first(roles, "db_schema")
    if schema:
        entities = _schema_entities(schema.path, schema.obj)

    # Screens from the UI↔API mapping, enriched with route paths if present.
    routes_df = _first(roles, "routes")
    routes = routes_df.obj if routes_df and isinstance(routes_df.obj, dict) else {}
    screens: list[dict[str, Any]] = []
    if mapping and screen_col and endpoint_col:
        screens = _screens_from_mapping(mapping.obj, screen_col, endpoint_col, routes)

    backend = _first(roles, "backend_structure")
    frontend = _first(roles, "frontend_structure")
    tags = sorted({e.get("tag", "") for e in endpoints if e.get("tag")})

    return ResolvedPack(
        endpoints=endpoints,
        entities=entities,
        screens=screens,
        backend_tree=_tree_root(backend.obj) if backend else {},
        frontend_tree=_tree_root(frontend.obj) if frontend else {},
        tag_reqs=_build_tag_reqs(roles, tags),
        roles={role: [df.path.name for df in items] for role, items in roles.items()},
    )


# --------------------------------------------------------------------------- agentic fallback

def llm_classify_unknown(pack_dir: str | Path) -> dict[str, str]:
    """Ask the LLM gateway to name the role of any file the detectors could not classify.

    Optional and best-effort: returns ``{filename: role}`` for unknown files, or ``{}`` if the
    gateway is unavailable. Kept out of :func:`resolve` so planning stays deterministic; callers
    can merge these hints when they want the adaptive "agent map" behavior on novel formats.
    """
    roles = detect_roles(pack_dir)
    unknown = roles.get("unknown") or []
    if not unknown:
        return {}
    try:
        from app.services.llm_gateway import LLMGateway  # local import: avoid hard dependency
    except Exception:  # noqa: BLE001
        return {}
    try:
        gateway = LLMGateway()
    except Exception:  # noqa: BLE001 - no API key / offline
        return {}

    known = "openapi, api_ui_mapping, db_schema, backend_structure, frontend_structure, requirements, user_features, routes"
    out: dict[str, str] = {}
    for df in unknown:
        try:
            snippet = df.path.read_text(encoding="utf-8-sig", errors="replace")[:1500]
        except OSError:
            continue
        prompt = (
            f"A design-package file is named {df.path.name!r}. Classify its ROLE as exactly one of: "
            f"{known}, or 'other'. Reply with only the role token.\n\n--- content ---\n{snippet}"
        )
        try:
            answer = gateway.complete(prompt=prompt, system="You label software design artifacts by role.")
        except Exception:  # noqa: BLE001
            continue
        token = _norm(answer).replace("_", "")
        for role in known.split(", "):
            if _norm(role) in token:
                out[df.path.name] = role
                break
    return out
