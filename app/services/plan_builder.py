"""Deterministic implementation-plan builder (no LLM).

Artifacts are identified by ROLE from their content — never by fixed filename — via
:mod:`app.services.design_pack`, so the API surface may arrive as OpenAPI or a flat CSV, the
schema as SQL DDL or JSON/Mongo, etc. From that normalized view it emits a list of
:class:`~app.models.work_item.WorkItem`.

Two builders, chosen by input shape:

* ADAPTIVE (the default when structure trees are present): the ``backend-structure.json`` /
  ``frontend-structure.json`` trees are authoritative — one item is emitted PER DIRECTORY so every
  file leaf is produced exactly once, and endpoints/tables/screens/req_ids are ATTACHED to those
  items as traceability + prompt-grounding context (they no longer decide which items exist).
* LEGACY (self-contained flat-CSV packs): per-operation/per-screen item generation is
  unchanged — one BACKEND item per operationId and one FRONTEND item per screen, grouped by
  (screen, layer) — but when structure trees are present, the overall output also runs the same
  completeness sweep as the adaptive path (see ``_reconcile_uncovered``), adding catch-all items
  for any shared/cross-cutting file (main.py, config/settings.py, App.tsx, ...) that no
  per-operation/per-screen item targets on its own.

Backend file-role detection is STACK-AGNOSTIC: files are classified by role (handler,
service, schema/DTO, model) via extension-agnostic name/path hints, so the same logic works for
NestJS/Express (``*.controller.ts``, ``*.service.ts``, ``dto/``, ``*.entity.ts``), FastAPI/Flask
(``router.py``/``routes.py``, ``service.py``, ``schemas.py``, ``models/``), Django, Rails, etc.
Synthesized filenames (per-op DTOs) and the fallback both use the module's dominant file
extension, so a Python backend yields ``.py`` targets and a TypeScript backend yields ``.ts`` —
NestJS packs render byte-for-byte identically to before.

Run ``python -m app.services.plan_builder`` to (re)generate
``app/tests/fixtures/implementation-plan.ecommerce.json``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.models import WorkItem
from app.services import design_pack

logger = logging.getLogger(__name__)

# app/services/plan_builder.py -> services -> app -> implementation -> services -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[4]
_PLAN_OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "implementation-plan.ecommerce.json"


def default_fixtures_dir() -> Path:
    """Repo ``fixtures/`` by default; override with the ``FIXTURES_DIR`` env var."""
    return Path(os.environ.get("FIXTURES_DIR", str(_REPO_ROOT / "fixtures")))


# --------------------------------------------------------------------------- helpers

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _table_names(schema_sql: str) -> list[str]:
    return re.findall(r"create\s+table\s+(?:if\s+not\s+exists\s+)?[`\"]?(\w+)", schema_sql, re.IGNORECASE)


def _table_stem(table: str) -> str:
    norm = _norm(table)
    return norm[:-1] if norm.endswith("s") else norm


def _tables_touched(endpoint_path: str, operation_id: str, tables: list[str]) -> list[str]:
    """Infer which tables an operation touches by matching table stems against the endpoint."""
    hay = _norm(endpoint_path + operation_id)
    return [t for t in tables if _table_stem(t) in hay]


def _walk(tree: dict, prefix: str = "") -> list[tuple[str, Any]]:
    """Flatten a structure.json ``tree`` into (path, value) leaves (dirs are keys ending '/')."""
    out: list[tuple[str, Any]] = []
    for key, value in tree.items():
        path = prefix + key
        if isinstance(value, dict):
            out.extend(_walk(value, path))
        else:
            out.append((path, value))
    return out


def _canonical_endpoint(method: str, path: str) -> str:
    return f"{method.strip().upper()} {path.split('?', 1)[0].strip()}"


# --------------------------------------------------------------------------- feature grouping

#: A user-features.md table row: ``| F-0X <name> | REQ-0YY | <user story> |``.
_FEATURE_MD_ROW = re.compile(r"^\|\s*(F-\d+)\b\s*([^|]*?)\s*\|\s*([A-Z]+-\d+)\s*\|", re.MULTILINE)


def _feature_map(pack: Path, roles: dict) -> dict[str, tuple[str, str]]:
    """Map ``requirement_id -> (feature_id, feature_title)`` from the pack's feature definition.

    Prefers the structured ``user_features`` artifact (``user_features.json`` — ``features[]``
    with ``id``/``name``/``requirements``); falls back to a ``user-features.md`` table
    (``| F-0X name | REQ | … |``). Returns ``{}`` when neither is present (items stay ungrouped).
    """
    mapping: dict[str, tuple[str, str]] = {}
    detected = design_pack._first(roles, "user_features")
    if detected is not None and isinstance(detected.obj, dict):
        for feat in detected.obj.get("features", []):
            if not isinstance(feat, dict):
                continue
            feature_id = str(feat.get("id") or feat.get("name") or "").strip()
            title = str(feat.get("name") or "").strip()
            if not feature_id:
                continue
            for req in feat.get("requirements", []):
                req = str(req).strip()
                if req:
                    mapping.setdefault(req, (feature_id, title))
    if mapping:
        return mapping

    md = pack / "user-features.md"
    if md.exists():
        for match in _FEATURE_MD_ROW.finditer(md.read_text(encoding="utf-8")):
            mapping.setdefault(match.group(3), (match.group(1), match.group(2).strip()))
    return mapping


def _assign_features(items: list[WorkItem], feature_map: dict[str, tuple[str, str]]) -> list[WorkItem]:
    """Return items tagged with the feature of their primary (first-listed) mapped requirement.

    Items whose requirements don't map to any feature are returned unchanged (feature_id stays "").
    """
    if not feature_map:
        return items
    tagged: list[WorkItem] = []
    for item in items:
        feature = next((feature_map[r] for r in item.requirement_ids if r in feature_map), None)
        if feature:
            tagged.append(item.model_copy(update={"feature_id": feature[0], "feature_title": feature[1]}))
        else:
            tagged.append(item)
    return tagged


# --------------------------------------------------------------------------- builders

def build_plan(pack_dir: str | Path) -> list[WorkItem]:
    """Build the deterministic implementation plan for a design pack.

    Artifacts are identified by ROLE from their content (see :mod:`app.services.design_pack`),
    so filenames and formats can vary between design hand-offs. Two shapes are supported:

    * the legacy flat mapping (a CSV carrying its own operation_id/req_ids/endpoint_path columns)
      → the original stack-agnostic builders, PLUS the same completeness sweep as the adaptive
      path (any structure-tree file no per-operation/per-screen item targets is swept into a
      catch-all item — see ``_reconcile_uncovered``);
    * anything else (OpenAPI + a UI↔API table + a SQL *or* JSON schema + structure trees)
      → the adaptive builders below.
    """
    pack = Path(pack_dir)
    roles = design_pack.detect_roles(pack)

    # Legacy path: a self-contained flat mapping keeps the original output byte-for-byte.
    rich = roles.get("rich_api_mapping")
    if rich:
        rows = rich[0].obj
        schema = design_pack._first(roles, "db_schema")
        tables = design_pack._schema_entities(schema.path, schema.obj) if schema else []
        backend = _structure_obj(roles, "backend_structure")
        frontend = _structure_obj(roles, "frontend_structure")
        backend_tree = backend.get("tree", {})
        frontend_tree = frontend.get("tree", {})
        items = _backend_items(rows, tables, backend) + _frontend_items(rows, frontend)
        # Authoritative-manifest guarantee (same guard the adaptive path already used below): the
        # legacy builders emit ONE item per operation/screen, so any file that isn't itself a
        # handler/service/schema/model tied to a SPECIFIC operation — shared/cross-cutting
        # infrastructure like main.py, config/settings.py, db/database.py, core/security.py, a
        # frontend App.tsx/main.tsx, api/client.ts, AuthContext, shared form components, ... —
        # is never assigned to any item and silently never gets generated. Sweep every structure-
        # tree leaf the per-operation/per-screen items didn't claim into directory-grouped
        # catch-all items, exactly like the adaptive path does. (Zero per-operation items is NOT
        # itself suspicious — a pack can legitimately have no REST surface, e.g. a client-only
        # game — so this reconciles on FILE coverage, not on whether any operation matched.)
        items += _reconcile_uncovered(backend_tree, frontend_tree, items)
    else:
        # Adaptive path: normalize whatever formats arrived, then build from the neutral view.
        resolved = design_pack.resolve(pack, roles)
        if resolved is None or (not resolved.endpoints and not resolved.screens):
            raise FileNotFoundError(
                f"{pack_dir}: no recognizable design-package artifacts (need an OpenAPI spec or a "
                "UI↔API mapping table)."
            )
        backend_tree, frontend_tree = resolved.backend_tree, resolved.frontend_tree
        items = _backend_items_adaptive(resolved) + _frontend_items_adaptive(resolved)
        # Authoritative-manifest guarantee: every non-test file leaf in the structure trees must be
        # produced by SOME work item. The builders are exhaustive by construction (one item per
        # directory), so this normally adds nothing — it's a guard that turns any future coverage
        # gap into an explicit catch-all item instead of a silent omission.
        items += _reconcile_uncovered(backend_tree, frontend_tree, items)

    # Final completeness check, run for BOTH paths: after the builders AND the reconcile sweep,
    # every non-test structure-tree leaf must be targeted by some item. This should be
    # structurally impossible to fail — reconcile sweeps whatever the builders didn't claim — so a
    # hit here means a leaf slipped past BOTH stages (a genuinely new, unanticipated gap, not the
    # already-fixed "zero per-operation matches" case, which reconcile already covers). Warn loudly
    # with the exact missing paths rather than silently ship an incomplete app again.
    still_missing = _missing_after_reconcile(backend_tree, frontend_tree, items)
    if still_missing:
        logger.warning(
            "%s: %d design file(s) are not targeted by any work item even after reconciliation — "
            "they will NOT be generated: %s", pack_dir, len(still_missing), ", ".join(still_missing),
        )

    # Tag each item with the user-feature it belongs to (feature → REQ traceability), so the commit
    # step can group items into ONE commit per feature. Items whose requirements don't map to any
    # feature (bootstrap/cross-cutting files) stay untagged and commit on their own.
    return _assign_features(items, _feature_map(pack, roles))


def _missing_after_reconcile(backend_tree: dict, frontend_tree: dict, items: list[WorkItem]) -> list[str]:
    """Non-test structure-tree leaves NOT targeted by any of ``items``, sorted. Empty in normal
    operation (``_reconcile_uncovered`` already swept everything it found) — a non-empty result
    means a leaf slipped past both the builders and the sweep, e.g. a future edit to either one."""
    targeted = {f for item in items for f in item.target_files}
    return sorted(
        p for tree in (backend_tree, frontend_tree) for p, _ in _source_leaves(tree) if p not in targeted
    )


def _structure_obj(roles: dict, role: str) -> dict:
    """Full structure JSON for the legacy builders, normalized to always carry a ``tree`` key.

    The expected shape is ``{"tree": {...}, "notes": ...}``, but some design hand-offs omit the
    wrapper and put the directory tree directly at the top level (e.g. a ``frontend-structure.json``
    shaped like ``{"auth-frontend/": {...}}`` with no ``"tree"`` key at all — seen in practice
    sitting right next to a properly-wrapped ``backend-structure.json`` in the SAME pack). Both
    ``_backend_items``/``_frontend_items`` read ``obj.get("tree", {})``, so an un-normalized bare
    tree silently resolves to an EMPTY tree — zero leaves, zero work items for that side — with no
    error at all; the plan just quietly omits that whole side of the app (e.g. no frontend). Detect
    the un-wrapped case (no ``"tree"`` key, or one whose value isn't itself a dict) and wrap it.
    """
    df = design_pack._first(roles, role)
    if not df or not isinstance(df.obj, dict):
        return {}
    obj = df.obj
    if isinstance(obj.get("tree"), dict):
        return obj
    return {"tree": obj}


# -- stack-agnostic file-role detection ------------------------------------
#
# A backend file's ROLE is inferred from extension-agnostic name/path hints, so the same
# classifier works across NestJS/Express (``*.controller.ts``, ``*.service.ts``, ``dto/``,
# ``*.entity.ts``), FastAPI/Flask (``router.py``, ``service.py``, ``schemas.py``, ``models/``),
# Django (``views.py``, ``serializers.py``, ``models.py``), Rails, etc. The hint sets are DATA —
# supporting a new stack means adding a hint here, not rewriting the builder.
_HANDLER_HINTS = ("controller", "router", "routes", "views", "handler")
_SERVICE_HINTS = ("service", "usecase", "interactor")
_SCHEMA_HINTS = ("dto", "schema", "serializer")
_MODEL_HINTS = ("entity", "model")            # note: "module" does NOT contain "model"
_MODEL_DIR_SEGMENTS = ("models", "entities", "entity", "model", "domain")
_MODEL_FILE_SUFFIXES = (".entity.ts", ".entity.js", ".model.ts", ".model.js", ".entity.py", ".model.py")


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1].lower()


def _ext(path: str) -> str:
    """Final file extension incl. dot (e.g. '.ts', '.py'); '' for dir leaves / extensionless."""
    if path.endswith("/"):
        return ""
    base = path.rsplit("/", 1)[-1]
    return "." + base.rsplit(".", 1)[-1] if "." in base else ""


def _dominant_ext(paths: list[str]) -> str:
    """Most common file extension among ``paths`` (ties broken by first occurrence)."""
    exts = [_ext(p) for p in paths]
    exts = [e for e in exts if e]
    return Counter(exts).most_common(1)[0][0] if exts else ""


def _is_dir_leaf(path: str) -> bool:
    return path.endswith("/")


def _has_hint(name: str, hints: tuple[str, ...]) -> bool:
    return any(h in name for h in hints)


def _is_handler(path: str) -> bool:
    return not _is_dir_leaf(path) and _has_hint(_basename(path), _HANDLER_HINTS)


def _is_service(path: str) -> bool:
    return not _is_dir_leaf(path) and _has_hint(_basename(path), _SERVICE_HINTS)


def _is_schema_dir(path: str) -> bool:
    return _is_dir_leaf(path) and _has_hint(_basename(path), _SCHEMA_HINTS)


def _is_schema_file(path: str) -> bool:
    return not _is_dir_leaf(path) and _has_hint(_basename(path), _SCHEMA_HINTS)


def _is_model(path: str) -> bool:
    if _is_dir_leaf(path):
        return False
    if _has_hint(_basename(path), _MODEL_HINTS):        # user.entity.ts, product.model.ts
        return True
    segments = path.lower().split("/")[:-1]              # a file inside a models/ or entities/ dir
    return any(seg in _MODEL_DIR_SEGMENTS for seg in segments)


def _model_stem(path: str) -> str:
    """Bare entity/model name, normalized and de-pluralized, for matching against a table stem."""
    base = _basename(path)
    for suffix in _MODEL_FILE_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    else:
        base = base.rsplit(".", 1)[0] if "." in base else base   # drop a plain .py/.ts/... extension
    norm = _norm(base)
    return norm[:-1] if norm.endswith("s") else norm


def _backend_items(rows: list[dict[str, str]], tables: list[str], backend: dict) -> list[WorkItem]:
    leaves = _walk(backend.get("tree", {}))
    handlers = {p: str(v) for p, v in leaves if _is_handler(p)}
    services = [p for p, _ in leaves if _is_service(p)]
    schema_dirs = [p for p, _ in leaves if _is_schema_dir(p)]
    schema_files = [p for p, _ in leaves if _is_schema_file(p)]
    models = [p for p, _ in leaves if _is_model(p)]
    default_ext = _dominant_ext([p for p, _ in leaves]) or ".ts"

    items: list[WorkItem] = []
    seen: set[str] = set()
    for row in rows:
        op = row.get("operation_id", "").strip()
        if not op or op == "-" or op in seen:
            continue
        seen.add(op)

        op_rows = [r for r in rows if r.get("operation_id", "").strip() == op]
        req_ids = sorted({rid.strip() for r in op_rows for rid in r["req_ids"].split(",") if rid.strip() and rid.strip() != "-"})
        endpoint = _canonical_endpoint(row["http_method"], row["endpoint_path"])
        touched = _tables_touched(row["endpoint_path"], op, tables)

        module_dir = _module_for_op(op, handlers)
        target_files = _backend_targets(
            op, module_dir, handlers, services, schema_dirs, schema_files, models, touched, default_ext
        )

        items.append(
            WorkItem(
                id=f"backend-{op}",
                requirement_ids=req_ids,
                endpoints=[endpoint],
                tables=touched,
                screens=[],
                target_files=target_files,
            )
        )
    return items


def _module_for_op(op: str, handlers: dict[str, str]) -> str:
    """The module dir (e.g. 'src/auth/' or 'app/auth/') whose handler description names this op."""
    for path, desc in handlers.items():
        if op in desc:
            return path.rsplit("/", 1)[0] + "/"
    return ""


def _backend_targets(
    op: str,
    module_dir: str,
    handlers: dict[str, str],
    services: list[str],
    schema_dirs: list[str],
    schema_files: list[str],
    models: list[str],
    touched: list[str],
    default_ext: str,
) -> list[str]:
    files: list[str] = []
    module_ext = default_ext
    if module_dir:
        module_handlers = [p for p in handlers if p.startswith(module_dir)]
        module_services = [p for p in services if p.startswith(module_dir)]
        module_ext = _dominant_ext(module_handlers + module_services) or default_ext
        files += module_handlers
        files += module_services
        # Schema/DTO: a per-op DTO synthesized from a dto/ DIRECTORY (NestJS-style), else the
        # module's existing shared schema FILE (schemas.py / serializers.py, Python/Django-style).
        schema_dir = next((d for d in schema_dirs if d.startswith(module_dir)), None)
        if schema_dir:
            files.append(f"{schema_dir}{op[:1].upper()}{op[1:]}Dto{module_ext}")
        else:
            files += [p for p in schema_files if p.startswith(module_dir)]
    # Data layer: model/entity files whose stem matches an inferred table (searched pack-wide,
    # since a module often reuses a model that lives under another module/dir).
    wanted = {_table_stem(t) for t in touched}
    files += [m for m in models if _model_stem(m) in wanted]
    # de-dup, preserve order; fall back to a single module-appropriate file if nothing matched
    return list(dict.fromkeys(files)) or [f"{module_dir}{op}{module_ext}"]


#: A page leaf's description starts with the literal word "route" then the route path — e.g.
#: "route /login" or, just as commonly in a real hand-off, "route /login: email + password, ...".
#: Capture ONLY the path token, stopping at whitespace OR a trailing punctuation mark (the colon
#: introducing the free-text explanation) — a blunt ``str.replace("route", "")`` (the old approach)
#: instead kept everything after the word "route", including ": <free-text explanation>", and a
#: naive ``\S+`` capture (an earlier attempt at this same fix) still swallowed a trailing ":" —
#: both produce a key nothing in the CSV ever matches.
_ROUTE_DESC_RE = re.compile(r"^route\s+([^\s:,;]+)")


def _route_key(text: str) -> str:
    """Normalize a route path for matching: drop a leading '/' and any trailing '/'. A structure
    tree's page description and a mapping CSV's ``route_id`` column commonly disagree on whether
    the leading slash is included ("/login" vs "login") — this makes both compare equal."""
    return text.strip().strip("/")


def _frontend_items(rows: list[dict[str, str]], frontend: dict) -> list[WorkItem]:
    leaves = _walk(frontend.get("tree", {}))
    page_by_route: dict[str, str] = {}
    for path, desc in leaves:
        if "/pages/" not in path or not path.endswith(".tsx"):
            continue
        m = _ROUTE_DESC_RE.match(str(desc))
        if m:
            page_by_route[_route_key(m.group(1))] = path
    api_files = [(path, str(desc)) for path, desc in leaves if "/api/" in path and path.endswith(".ts")]

    items: list[WorkItem] = []
    seen: set[str] = set()
    for row in rows:
        route_id = _route_key(row.get("route_id", ""))
        screen = row.get("screen", "").strip()
        if not route_id or route_id in seen or route_id not in page_by_route:
            continue  # skip globals / logout / anything without a real page
        seen.add(route_id)

        screen_rows = [r for r in rows if _route_key(r.get("route_id", "")) == route_id]
        req_ids = sorted({rid.strip() for r in screen_rows for rid in r["req_ids"].split(",") if rid.strip() and rid.strip() != "-"})
        ops = {r.get("operation_id", "").strip() for r in screen_rows}
        target_files = [page_by_route[route_id]]
        target_files += [p for p, desc in api_files if any(op and op in desc for op in ops)]

        items.append(
            WorkItem(
                id=f"frontend-{route_id}",
                requirement_ids=req_ids,
                endpoints=[],
                tables=[],
                screens=[screen],
                target_files=list(dict.fromkeys(target_files)),
            )
        )
    return items


# ------------------------------------------------------------------ adaptive builders
#
# Used when the design pack is NOT the legacy flat CSV: endpoints come from OpenAPI, entities
# from a SQL or JSON schema, screens from a UI↔API table. Target files are located in the
# real structure trees with the same stack-agnostic role classifier used above.

def _is_test_path(path: str) -> bool:
    base = _basename(path)
    if ".test." in base or ".spec." in base or base.startswith("test_") or base.endswith((".test", "_test")):
        return True
    segs = path.lower().split("/")
    return "tests" in segs or "__tests__" in segs


def _is_validator(path: str) -> bool:
    return not _is_dir_leaf(path) and "validator" in _basename(path)


def _is_page(path: str) -> bool:
    if _is_dir_leaf(path):
        return False
    base = _basename(path)
    if not base.endswith((".jsx", ".tsx", ".vue")) or ".test." in base or ".module." in base:
        return False
    return "pages" in path.lower().split("/")


def _is_api_service(path: str) -> bool:
    if _is_dir_leaf(path) or _is_test_path(path):
        return False
    base = _basename(path)
    if not base.endswith((".js", ".ts")) or ".module." in base:
        return False
    segs = path.lower().split("/")
    return "services" in segs or "api" in segs


def _synth_op(method: str, path: str) -> str:
    parts = [p for p in re.split(r"[/{}]", path) if p]
    return method.lower() + "".join(p[:1].upper() + p[1:] for p in parts)


def _module_dir_for_tag(tag: str, paths: list[str]) -> str:
    """Deepest directory whose name matches an OpenAPI tag (e.g. tag 'auth' → '.../modules/auth/')."""
    stem = _singular(_norm(tag))
    if not stem:
        return ""
    best = ""
    for p in paths:
        segs = p.split("/")[:-1]
        for i, seg in enumerate(segs):
            sn = _singular(_norm(seg))
            # Exact (singularized) match only. A substring test here over-matches — `auth`
            # would attach to `authors/`, `art` to `cart/` — misrouting an endpoint/table/req to
            # the wrong module. Unmatched tags are dropped (documented), which is safe: this only
            # attaches traceability metadata; directory grouping (not this fn) owns file coverage.
            if sn and sn == stem:
                candidate = "/".join(segs[: i + 1]) + "/"
                if len(candidate) > len(best):
                    best = candidate
    return best


def _singular(norm: str) -> str:
    return norm[:-1] if norm.endswith("s") and len(norm) > 1 else norm


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-") or "screen"


# -- tree-driven grouping --------------------------------------------------
#
# The structure trees (backend-structure.json / frontend-structure.json) are the AUTHORITATIVE
# manifest of what to build. We walk the tree and emit ONE work item per directory, so every
# file leaf is produced by some item — including cross-cutting/bootstrap files (app entrypoints,
# config, middleware, utils, routers, stores, styles) and endpoint-less/screen-less modules that
# a per-endpoint or per-screen planner would never reach. Endpoints/tables/screens/req_ids are
# ATTACHED to items as traceability + prompt-grounding context; they no longer decide which
# items exist. One item per module also means each shared file (e.g. orders.controller.js) is
# generated exactly once with all its endpoints in context — never clobbered by sibling items.


def _dir_of(path: str) -> str:
    """Immediate parent directory of a file leaf, trailing slash included ('' if top-level)."""
    return path.rsplit("/", 1)[0] + "/" if "/" in path else ""


def _group_by_dir(leaves: list[tuple[str, Any]]) -> list[tuple[str, list[tuple[str, Any]]]]:
    """Group file leaves by their immediate parent directory, preserving first-seen order."""
    groups: dict[str, list[tuple[str, Any]]] = {}
    order: list[str] = []
    for path, desc in leaves:
        d = _dir_of(path)
        if d not in groups:
            groups[d] = []
            order.append(d)
        groups[d].append((path, desc))
    return [(d, groups[d]) for d in order]


def _dir_item_slug(directory: str) -> str:
    """Stable, readable work-item slug for a directory (drops the top project dir + src/app noise)."""
    segs = [s for s in directory.strip("/").split("/") if s]
    if len(segs) > 1:
        segs = segs[1:]                      # drop the top project dir (quickbite-backend/…)
    while segs and segs[0] in ("src", "app"):
        segs = segs[1:]                      # drop conventional source-root noise
    return _slug("-".join(segs)) if segs else "root"


def _source_leaves(tree: dict) -> list[tuple[str, Any]]:
    """File leaves of a structure tree, excluding directory entries and test files."""
    return [(p, d) for p, d in _walk(tree) if not _is_dir_leaf(p) and not _is_test_path(p)]


def _items_from_groups(
    prefix: str,
    leaves: list[tuple[str, Any]],
    *,
    endpoints_by_dir: dict[str, list[str]] | None = None,
    tables_by_dir: dict[str, set[str]] | None = None,
    screens_by_dir: dict[str, list[str]] | None = None,
    reqs_by_dir: dict[str, set[str]] | None = None,
    used_ids: set[str] | None = None,
) -> list[WorkItem]:
    """Build one WorkItem per directory group, attaching whatever metadata maps to that dir."""
    endpoints_by_dir = endpoints_by_dir or {}
    tables_by_dir = tables_by_dir or {}
    screens_by_dir = screens_by_dir or {}
    reqs_by_dir = reqs_by_dir or {}
    used_ids = used_ids if used_ids is not None else set()

    items: list[WorkItem] = []
    for directory, group in _group_by_dir(leaves):
        base = f"{prefix}-{_dir_item_slug(directory)}"
        item_id, n = base, 1
        while item_id in used_ids:           # keep ids unique if two dirs slug the same
            n += 1
            item_id = f"{base}-{n}"
        used_ids.add(item_id)
        items.append(
            WorkItem(
                id=item_id,
                requirement_ids=sorted(reqs_by_dir.get(directory, set())),
                endpoints=sorted(dict.fromkeys(endpoints_by_dir.get(directory, []))),
                tables=sorted(tables_by_dir.get(directory, set())),
                screens=list(screens_by_dir.get(directory, [])),
                target_files=[p for p, _ in group],
                file_specs={p: str(desc) for p, desc in group},
            )
        )
    return items


def _backend_items_adaptive(resolved: "design_pack.ResolvedPack") -> list[WorkItem]:
    leaves = _source_leaves(resolved.backend_tree)
    if not leaves:
        return []
    handlers = [p for p, _ in leaves if _is_handler(p)]
    services = [p for p, _ in leaves if _is_service(p)]
    module_search = handlers + services
    entities = resolved.entities

    # Aggregate each endpoint's traceability onto its module directory (metadata, not the driver).
    endpoints_by_dir: dict[str, list[str]] = defaultdict(list)
    tables_by_dir: dict[str, set[str]] = defaultdict(set)
    reqs_by_dir: dict[str, set[str]] = defaultdict(set)
    for ep in resolved.endpoints:
        op = ep.get("operation_id") or _synth_op(ep["method"], ep["path"])
        module_dir = _module_dir_for_tag(ep.get("tag", ""), module_search)
        if not module_dir:
            first_seg = re.sub(r"\{[^}]+\}", "", ep["path"]).strip("/").split("/")
            module_dir = _module_dir_for_tag(first_seg[0] if first_seg and first_seg[0] else "", module_search)
        if not module_dir:
            continue  # unresolved endpoint: its module files are still generated from the tree
        endpoints_by_dir[module_dir].append(_canonical_endpoint(ep["method"], ep["path"]))
        tables_by_dir[module_dir].update(_tables_touched(ep["path"], op, entities))
        reqs_by_dir[module_dir].update(resolved.tag_reqs.get(ep.get("tag", ""), []))

    return _items_from_groups(
        "backend",
        leaves,
        endpoints_by_dir=endpoints_by_dir,
        tables_by_dir=tables_by_dir,
        reqs_by_dir=reqs_by_dir,
    )


def _frontend_items_adaptive(resolved: "design_pack.ResolvedPack") -> list[WorkItem]:
    leaves = _source_leaves(resolved.frontend_tree)
    if not leaves:
        return []
    pages = [p for p, _ in leaves if _is_page(p)]
    ep_index = resolved.endpoint_by_key()

    # Attach each screen (and the req_ids behind its endpoints) to the directory of its page file.
    screens_by_dir: dict[str, list[str]] = defaultdict(list)
    reqs_by_dir: dict[str, set[str]] = defaultdict(set)
    for screen in resolved.screens:
        page = _match_page(screen["name"], pages)
        if not page:
            continue
        directory = _dir_of(page)
        if screen["name"] not in screens_by_dir[directory]:
            screens_by_dir[directory].append(screen["name"])
        for key in screen.get("endpoints", []):
            tag = ep_index.get(key, {}).get("tag", "")
            reqs_by_dir[directory].update(resolved.tag_reqs.get(tag, []))

    return _items_from_groups(
        "frontend",
        leaves,
        screens_by_dir=screens_by_dir,
        reqs_by_dir=reqs_by_dir,
    )


def _reconcile_uncovered(
    backend_tree: dict, frontend_tree: dict, items: list[WorkItem]
) -> list[WorkItem]:
    """Sweep any structure-tree file not already covered by an item into a catch-all item.

    Shared by both builders. For the adaptive builders this is normally a no-op (they emit one
    item per directory, covering every leaf) — a guard against a future filtering change silently
    dropping files. For the LEGACY (per-operation/per-screen) builders it does real work: those
    only ever target files tied to a SPECIFIC operation/screen, so shared/cross-cutting
    infrastructure (main.py, config/settings.py, core/security.py, a frontend App.tsx, api/
    client.ts, ...) is never assigned to any item on its own — this is what sweeps those in.
    """
    covered = {f for item in items for f in item.target_files}
    used_ids = {item.id for item in items}
    extra: list[WorkItem] = []
    for prefix, tree in (("backend", backend_tree), ("frontend", frontend_tree)):
        uncovered = [(p, d) for p, d in _source_leaves(tree) if p not in covered]
        if uncovered:
            extra += _items_from_groups(prefix, uncovered, used_ids=used_ids)
    return extra


def _match_page(screen_name: str, pages: list[str]) -> str:
    """Best page component for a screen by token subset (e.g. 'Customer Login' → 'LoginPage.jsx')."""
    from app.services.design_pack import _tokens

    stoks = _tokens(screen_name) - {"page", "screen", "view"}
    best, best_score = "", 0
    for p in pages:
        # Tokenize the ORIGINAL-CASE stem so _tokens can split camelCase/PascalCase
        # ("LoginPage" → {"login", "page"}). Using _basename here would lowercase first, gluing it
        # into {"loginpage"} and defeating the subset test below. _tokens lowercases internally.
        stem = p.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        ptoks = _tokens(stem) - {"page", "screen", "view", "module", "test"}
        if not ptoks or not ptoks <= stoks:
            continue
        score = len(ptoks & stoks) + (1 if "page" in _basename(p).lower() else 0)
        if score > best_score:
            best, best_score = p, score
    return best


# --------------------------------------------------------------------------- write

def write_plan(pack_dir: str | Path, out_path: str | Path = _PLAN_OUT) -> Path:
    """Build the plan for ``pack_dir`` and write it to ``out_path`` as JSON."""
    plan = build_plan(pack_dir)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"work_items": [item.model_dump() for item in plan]}
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out


def main() -> None:
    pack = default_fixtures_dir() / "ecommerce_complete"
    out = write_plan(pack)
    print(f"wrote {out} ({len(build_plan(pack))} work items)")


if __name__ == "__main__":
    main()
