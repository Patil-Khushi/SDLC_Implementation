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
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # PyYAML is used for OpenAPI specs; degrade gracefully if unavailable.
    import yaml
except Exception:  # pragma: no cover - yaml is in requirements, this is just belt-and-braces
    yaml = None  # type: ignore[assignment]

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
        text = path.read_text(encoding="utf-8")
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
            return yaml.safe_load(text)
        except Exception:  # noqa: BLE001 - malformed YAML is just "not structured"
            return None
    # Unknown extension: try JSON then YAML so a mislabeled file is still understood.
    try:
        return json.loads(text)
    except ValueError:
        pass
    if yaml is not None:
        try:
            return yaml.safe_load(text)
        except Exception:  # noqa: BLE001
            return None
    return None


def _read_table(path: Path) -> list[dict[str, str]]:
    """Read a delimited table (csv/tsv) into row dicts; ``[]`` if it is not clearly tabular."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
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


# --------------------------------------------------------------------------- role detection

def _is_openapi(obj: Any) -> bool:
    return isinstance(obj, dict) and ("openapi" in obj or "swagger" in obj) and isinstance(obj.get("paths"), dict)


def _schema_entities(path: Path, obj: Any) -> list[str]:
    """Entity/table names from either SQL DDL or a JSON schema; ``[]`` if it is not a schema."""
    if path.suffix.lower() == ".sql" or (isinstance(obj, str) and "create table" in obj.lower()):
        try:
            sql = path.read_text(encoding="utf-8")
        except OSError:
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
    if "frontend" in name or "client" in name or "ui" in name or "web" in name:
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
        if not path.is_file():
            continue
        suffix = path.suffix.lower()

        # 1. Delimited tables (csv/tsv) → UI↔API mapping.
        if suffix in (".csv", ".tsv") and _record_table(path):
            continue

        # 2. SQL schema.
        if suffix == ".sql":
            roles["db_schema"].append(DetectedFile(path, "db_schema", None))
            continue

        # 3. Structured (JSON/YAML) files — parsed FIRST so JSON isn't mistaken for CSV.
        obj = _load_structured(path)
        if obj is not None:
            if _is_openapi(obj):
                roles["openapi"].append(DetectedFile(path, "openapi", obj))
            elif _is_user_features(obj):
                roles["user_features"].append(DetectedFile(path, "user_features", obj))
            elif _is_requirements(obj):
                roles["requirements"].append(DetectedFile(path, "requirements", obj))
            elif _schema_entities(path, obj):
                roles["db_schema"].append(DetectedFile(path, "db_schema", obj))
            elif _is_routes_map(obj):
                roles["routes"].append(DetectedFile(path, "routes", obj))
            elif _looks_like_tree(obj):
                side = _structure_side(path, path.read_text(encoding="utf-8", errors="replace"))
                roles[side].append(DetectedFile(path, side, obj))
            else:
                roles.setdefault("unknown", []).append(DetectedFile(path, "unknown", obj))
            continue

        # 4. Fallback: a table with a non-standard extension (e.g. .txt, no extension).
        if _record_table(path):
            continue

        roles.setdefault("unknown", []).append(DetectedFile(path, "unknown", None))

    return roles


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
    """Map each OpenAPI tag to requirement ids via the feature whose name it matches."""
    feature_reqs: dict[str, list[str]] = {}   # feature-name-norm → [REQ]
    uf = _first(roles, "user_features")
    if uf and isinstance(uf.obj, dict):
        for feat in uf.obj.get("features", []):
            if isinstance(feat, dict) and feat.get("name"):
                reqs = [str(r) for r in feat.get("requirements", []) if r]
                if reqs:
                    feature_reqs[_norm(feat["name"])] = reqs
    if not feature_reqs:  # fall back to requirements grouped by their 'feature' field
        req = _first(roles, "requirements")
        block = (req.obj.get("functional") if req and isinstance(req.obj, dict) else req.obj) if req else None
        if isinstance(block, list):
            grouped: dict[str, list[str]] = defaultdict(list)
            for item in block:
                if isinstance(item, dict) and item.get("feature") and item.get("id"):
                    grouped[_norm(item["feature"])].append(str(item["id"]))
            feature_reqs = dict(grouped)

    tag_reqs: dict[str, list[str]] = {}
    for tag in tags:
        if not tag:
            continue
        stem = _singular(_norm(tag))
        matched: list[str] = []
        for fname, reqs in feature_reqs.items():
            if stem and stem in fname:
                matched.extend(reqs)
        if matched:
            tag_reqs[tag] = sorted(dict.fromkeys(matched))
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
            snippet = df.path.read_text(encoding="utf-8", errors="replace")[:1500]
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
