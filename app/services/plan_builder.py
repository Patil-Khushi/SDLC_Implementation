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
  Leaves the deterministic scaffold already renders (the jest/babel test harness — see
  ``_SCAFFOLD_OWNED_RE``) are held back: they are produced by ``scaffold_node``, and letting a work
  item target them would only let the LLM overwrite a known-correct file with a guess.
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
from app.services.boilerplate import _jest_harness_plan, resolve_scaffold_config

logger = logging.getLogger(__name__)

# app/services/plan_builder.py -> services -> app -> implementation -> services -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[4]
_PLAN_OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "implementation-plan.ecommerce.json"


def default_fixtures_dir() -> Path:
    """Repo ``fixtures/`` by default; override with the ``FIXTURES_DIR`` env var."""
    return Path(os.environ.get("FIXTURES_DIR", str(_REPO_ROOT / "fixtures")))


def _load_pack_artifacts(pack: Path) -> dict[str, Any]:
    """Pack files as a ``name -> content`` dict (.json parsed, else text) — the same shape
    :func:`app.services.boilerplate.resolve_scaffold_config` expects. Delegates to the ONE loader
    every caller now shares (:func:`app.services.pack_index.load_pack`).

    Used ONLY by :func:`_harness_covered_sides` to answer "will the scaffold actually render a
    Jest harness for this side of the pack?" — never fatal on its own; a pack this can't read
    just means that question falls back to its conservative default (see there).
    """
    from app.services.pack_index import load_pack  # local: pack_index imports design_pack, as we do

    return load_pack(pack)


def _harness_covered_sides(pack: Path) -> tuple[bool, bool]:
    """(backend_covered, frontend_covered): whether ``app.services.boilerplate`` will actually
    render a deterministic Jest test harness for that SIDE of this pack.

    Why this matters here: :func:`_source_leaves` excludes jest.config/babel.config/jest.setup
    leaves from every work item on the assumption the scaffold produces them instead (see
    ``_SCAFFOLD_OWNED_RE``). That assumption can be wrong — a structure tree can list a
    jest.config.js leaf under a side whose resolved capabilities never declare ``jest`` as a
    devDependency, in which case ``_jest_harness_plan`` renders nothing for it — and until this
    check existed nothing verified the two decisions agreed, so such a leaf was excluded from work
    items AND never scaffolded: silently never generated at all.

    Deliberately PER-SIDE, not per-exact-root: ``_jest_harness_plan``'s ``root`` is expressed in
    the ORIGINAL (pre-rerooting) tree namespace (e.g. ``quickbite-backend/`` or ``""``), while the
    adaptive builders here re-root every leaf under canonical ``backend/``/``frontend/`` folders
    (see ``_reroot``) — those are two different path namespaces for the same project, and matching
    on the exact root string would either never fire (if the scaffold's un-rerooted root doesn't
    happen to equal ``"backend/"``/``"frontend/"``) or require reconciling that rerooting mismatch
    too, which is a bigger change than this check. Per-side is the granularity both sides of the
    mismatch can agree on without touching either one.

    Best-effort and conservative: a resolution failure (unreadable pack, malformed capabilities
    artifact) returns ``(True, True)`` — i.e. keep today's blanket exclusion — rather than
    ``(False, False)``, because generating a harness config file with the LLM re-opens the exact
    bug this exclusion exists to prevent (a single-sided work item guessing a jest.config that
    breaks the other side). Only a POSITIVELY CONFIRMED absence of a planned harness un-excludes a
    leaf.
    """
    try:
        cfg = resolve_scaffold_config(pack.name, _load_pack_artifacts(pack))
        owners = {h.owner for h in _jest_harness_plan(cfg.data)}
    except Exception:
        logger.exception(
            "plan_builder: could not resolve the scaffold's jest-harness plan for %s — "
            "conservatively treating every scaffold-owned leaf as covered", pack,
        )
        return True, True
    return "backend" in owners, "frontend" in owners


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
    # Whether the scaffold will actually render a Jest harness for each side — see
    # _harness_covered_sides. Resolved ONCE, up front, and threaded through every call below that
    # decides whether a jest.config/babel.config/jest.setup leaf is scaffold-owned (excluded) or
    # ordinary (generated): the builders' own exclusion, the completeness sweep, and the final
    # completeness check must all agree, or a leaf can vanish from both without ever being flagged.
    backend_covers, frontend_covers = _harness_covered_sides(pack)

    # The adaptive path re-roots every file under canonical backend/ , frontend/ folders (a clean
    # two-folder project regardless of how the pack wrapped its trees); the legacy path keeps its
    # byte-for-byte paths. This flag threads that choice through the shared reconcile + completeness
    # helpers below.
    reroot_paths = False

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
        items += _reconcile_uncovered(
            backend_tree, frontend_tree, items,
            backend_covers=backend_covers, frontend_covers=frontend_covers,
        )
    else:
        # Adaptive path: normalize whatever formats arrived, then build from the neutral view.
        resolved = design_pack.resolve(pack, roles)
        if resolved is None or (not resolved.endpoints and not resolved.screens):
            raise FileNotFoundError(
                f"{pack_dir}: no recognizable design-package artifacts (need an OpenAPI spec or a "
                "UI↔API mapping table)."
            )
        backend_tree, frontend_tree = resolved.backend_tree, resolved.frontend_tree
        reroot_paths = True  # the adaptive builders emit canonical backend/ , frontend/ paths
        unexpanded = _synthesized_dir_leaves(backend_tree)[1] + _synthesized_dir_leaves(frontend_tree)[1]
        fonts = [p for p, _ in _walk(frontend_tree) + _walk(backend_tree) if _is_asset_dir_leaf(p) and "font" in _basename(p)]
        if unexpanded:
            logger.warning(
                "%s: %d directory entr(y/ies) in the structure name a folder but no files, so nothing "
                "is generated for them: %s", pack_dir, len(unexpanded), "; ".join(unexpanded),
            )
        if fonts:
            logger.warning(
                "%s: font folder(s) %s are external assets - the generator cannot produce binary font "
                "files; the app falls back to the font-family stack in the design tokens until they "
                "are supplied.", pack_dir, ", ".join(fonts),
            )
        items = (
            _backend_items_adaptive(resolved, harness_covers=backend_covers)
            + _frontend_items_adaptive(resolved, harness_covers=frontend_covers)
        )
        # Authoritative-manifest guarantee: every non-test, non-scaffold-owned file leaf in the
        # structure trees must be produced by SOME work item. The builders are exhaustive by
        # construction (one item per directory), so this normally adds nothing — it's a guard that
        # turns any future coverage gap into an explicit catch-all item instead of a silent omission.
        items += _reconcile_uncovered(
            backend_tree, frontend_tree, items, reroot=True,
            backend_covers=backend_covers, frontend_covers=frontend_covers,
        )
        # Traceability check: a functional requirement on no item is generated by nobody in
        # particular — every prompt sees it as someone else's job. (Business rules and NFRs are
        # cross-cutting by nature and reach prompts as text, not as item ids.)
        on_items = {rid for item in items for rid in item.requirement_ids}
        unplaced = [rid for rid in _functional_req_ids(roles) if rid not in on_items]
        if unplaced:
            logger.warning(
                "%s: %d functional requirement(s) land on no work item, so no file is generated "
                "with them in view: %s", pack_dir, len(unplaced), ", ".join(unplaced),
            )

    # Final completeness check, run for BOTH paths: after the builders AND the reconcile sweep,
    # every non-test structure-tree leaf must be targeted by some item. This should be
    # structurally impossible to fail — reconcile sweeps whatever the builders didn't claim — so a
    # hit here means a leaf slipped past BOTH stages (a genuinely new, unanticipated gap, not the
    # already-fixed "zero per-operation matches" case, which reconcile already covers). Warn loudly
    # with the exact missing paths rather than silently ship an incomplete app again.
    still_missing = _missing_after_reconcile(
        backend_tree, frontend_tree, items, reroot=reroot_paths,
        backend_covers=backend_covers, frontend_covers=frontend_covers,
    )
    if still_missing:
        logger.warning(
            "%s: %d design file(s) are not targeted by any work item even after reconciliation — "
            "they will NOT be generated: %s", pack_dir, len(still_missing), ", ".join(still_missing),
        )

    # Tag each item with the user-feature it belongs to (feature → REQ traceability), so the commit
    # step can group items into ONE commit per feature. Items whose requirements don't map to any
    # feature (bootstrap/cross-cutting files) stay untagged and commit on their own.
    return _assign_features(items, _feature_map(pack, roles))


def _functional_req_ids(roles: dict) -> list[str]:
    """Ids of the pack's functional requirements, in pack order (a ``functional`` list, or a bare
    list of requirement objects). Empty when the pack has no requirements artifact."""
    hit = design_pack._first(roles, "requirements")
    obj = hit.obj if hit else None
    rows = obj if isinstance(obj, list) else (obj.get("functional") or []) if isinstance(obj, dict) else []
    return list(dict.fromkeys(str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")))


def _missing_after_reconcile(
    backend_tree: dict, frontend_tree: dict, items: list[WorkItem], *,
    reroot: bool = False, backend_covers: bool = True, frontend_covers: bool = True,
) -> list[str]:
    """Generatable structure-tree leaves NOT targeted by any of ``items``, sorted (tests and
    scaffold-owned harness configs are not generatable — see :func:`_source_leaves`). Empty in
    normal operation (``_reconcile_uncovered`` already swept everything it found) — a non-empty
    result means a leaf slipped past both the builders and the sweep, e.g. a future edit to
    either one.

    ``reroot`` compares in the canonical ``backend/`` / ``frontend/`` form the adaptive builders
    emit; the legacy path leaves it off so its byte-for-byte paths are compared as-is.
    ``backend_covers``/``frontend_covers`` (see :func:`_harness_covered_sides`) must match what the
    builders themselves used, or a scaffold-owned leaf excluded there but not here would show up
    as a false "missing" (or the reverse: included there but excluded here would hide a real gap).
    """
    targeted = {f for item in items for f in item.target_files}
    if reroot:
        expected = {p for p, _ in _rerooted_source_leaves(backend_tree, "backend", harness_covers=backend_covers)}
        expected |= {p for p, _ in _rerooted_source_leaves(frontend_tree, "frontend", harness_covers=frontend_covers)}
    else:
        expected = {p for p, _ in _source_leaves(backend_tree, harness_covers=backend_covers)}
        expected |= {p for p, _ in _source_leaves(frontend_tree, harness_covers=frontend_covers)}
    return sorted(p for p in expected if p not in targeted)


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
    """True for a test-file path: colocated JS-style (``*.test.js``/``*.spec.js``), Python's
    ``test_*.py`` prefix OR ``*_test.py`` suffix convention, or any path under a ``tests/``/
    ``__tests__/`` directory.

    The suffix form needs the extension stripped before the ``endswith`` check, or it never
    matches: ``"login_test.py"``'s basename ends in ``".py"``, not ``"_test"``, so the previous
    ``base.endswith((".test", "_test"))`` (checked against the FULL basename, extension included)
    only ever matched an extensionless ``foo_test`` — never the actual ``*_test.py`` convention it
    was meant to catch. The prefix form (``startswith("test_")``) doesn't have this problem — a
    prefix check is extension-agnostic — so it stays on the full basename, unchanged.
    """
    base = _basename(path)
    stem = base.rsplit(".", 1)[0] if "." in base else base
    if ".test." in base or ".spec." in base or base.startswith("test_") or stem.endswith("_test"):
        return True
    segs = path.lower().split("/")
    return "tests" in segs or "__tests__" in segs


#: Test-harness config files the DETERMINISTIC scaffold owns (rendered by
#: :mod:`app.services.boilerplate` from templates whose correct shape is already known:
#: ``jest.config.cjs``, ``babel.config.cjs``, ``jest.setup.cjs``).
#:
#: They must NOT appear in any work item's ``target_files``. ``scaffold_node`` writes them BEFORE
#: code generation, so a work item listing one has the LLM silently overwrite the good version with
#: a guessed one — and the guess is made from inside a single-sided work item that cannot see the
#: whole project. That is exactly how a full-stack repo shipped a backend-framed jest.config.js
#: (``testEnvironment: 'node'``, a testMatch catching only ``*.test.js``, no Babel config at all)
#: over a React frontend: 103 of 235 test files were never collected and every JSX/ESM test failed
#: to parse. Matching is by NAME, not extension, so a ``.js``/``.cjs``/``.mjs``/``.ts``/``.json``
#: variant is excluded just the same.
_SCAFFOLD_OWNED_RE = re.compile(
    r"^(?:jest\.config|jest\.setup|babel\.config)\.[cm]?[jt]s(?:on)?$"
    r"|^\.babelrc(?:\.[a-z]+)?$"
)


def _is_scaffold_owned(path: str) -> bool:
    """True for a file the deterministic scaffold renders, so no work item should target it."""
    return not _is_dir_leaf(path) and bool(_SCAFFOLD_OWNED_RE.match(_basename(path)))


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


# -- design-pack asset synthesis -------------------------------------------
#
# Structure trees list bulk asset folders as DIRECTORY leaves with a prose description
# (e.g. "assets/icons/": "SVG icon files …") instead of enumerating each file. Directory leaves
# are otherwise dropped, so without this expansion no asset file is ever planned and every
# generated ``import x from '@/assets/…'`` dangles (Vite build error). We synthesize concrete
# ``.svg`` targets from the folder description; the code generator fills their content, reusing the
# inline <svg> icons the mockup already provides. Binary asset targets the text-only generator
# cannot emit as valid bytes (favicon.ico) are normalized to their SVG equivalent.

#: Curated icon set for an ``assets/icons/`` folder — the common UI affordances a storefront/admin
#: front-end references (nav, catalogue, cart, account, status). The generator reuses the mockup's
#: matching inline <svg> where one exists and draws the rest in the same 24×24 stroke style.
_DEFAULT_ICON_SET = (
    "menu", "close", "search", "cart", "user", "bell", "heart", "star",
    "chevron-down", "chevron-left", "chevron-right", "check", "trash", "edit",
    "plus", "minus", "external-link", "map-pin", "package",
)

#: Fallback image asset names when a folder description names none.
_DEFAULT_IMAGE_SET = ("logo", "placeholder", "empty-state")


def _is_asset_dir_leaf(path: str) -> bool:
    """A directory leaf (path ends '/') sitting under an ``assets/`` segment."""
    return _is_dir_leaf(path) and "assets" in path.lower().split("/")


def _nouns_from_desc(desc: str) -> list[str]:
    """Comma-separated asset names from a folder description, or ``[]`` if it names none.

    A parenthetical list is the name source when present; otherwise a top-level comma list of 2+
    items. A single descriptive phrase ("Static image assets") is NOT a filename list -> [].

    "Static image assets (logo, placeholder product image, empty-state illustrations)"
        -> ["logo", "placeholder product image", "empty-state illustrations"]
    "Static image assets" -> []
    """
    m = re.search(r"\(([^)]*)\)", desc)
    if m:
        return [part.strip() for part in m.group(1).split(",") if part.strip()]
    parts = [part.strip() for part in desc.split(",") if part.strip()]
    return parts if len(parts) >= 2 else []


def _expand_asset_leaf(path: str, desc: str) -> list[tuple[str, str]]:
    """Expand an ``assets/…/`` directory leaf into concrete ``.svg`` (path, spec) leaves.

    Icon folders use the icon names their description lists ("Status and UI icon SVGs (success,
    warning, error, info)") and fall back to :data:`_DEFAULT_ICON_SET` only when it names none —
    the default is a storefront's set (cart, heart, package), which a fleet-maintenance app has no
    use for. Font folders produce NOTHING: a font is a binary the text-only generator cannot emit,
    and an SVG named like a font is worse than no file (see :func:`_source_leaves`, which reports
    them as external assets).
    """
    base = path if path.endswith("/") else path + "/"
    if "font" in _basename(path):
        return []
    if "icon" in _basename(path):
        named = [_slug(re.sub(r"\b(icons?|svgs?)\b", "", n, flags=re.I).strip() or n) for n in _nouns_from_desc(desc)]
        icons = list(dict.fromkeys(n for n in named if n and n != "screen")) or list(_DEFAULT_ICON_SET)
        return [
            (
                f"{base}{icon}.svg",
                f"Standalone SVG icon '{icon}' (24×24 viewBox, currentColor stroke matching the "
                f"design tokens). Reuse the matching inline <svg> from the mockup if one exists.",
            )
            for icon in icons
        ]
    nouns = _nouns_from_desc(desc)
    leaves: list[tuple[str, str]] = []
    seen: set[str] = set()
    for noun in nouns:
        cleaned = re.sub(r"\b(image|images|illustration|illustrations|asset|assets)\b", "", noun, flags=re.I).strip()
        slug = _slug(cleaned or noun)
        if slug in seen:
            continue
        seen.add(slug)
        leaves.append((f"{base}{slug}.svg", f"SVG asset for '{noun}' (self-contained, uses design-token colours)."))
    if not leaves:
        leaves = [(f"{base}{name}.svg", f"SVG {name} asset (self-contained, uses design-token colours).") for name in _DEFAULT_IMAGE_SET]
    return leaves


def _asset_leaves(tree: dict) -> list[tuple[str, str]]:
    """Concrete ``.svg`` leaves synthesized from every ``assets/…/`` directory leaf in ``tree``."""
    out: list[tuple[str, str]] = []
    for path, desc in _walk(tree):
        if _is_asset_dir_leaf(path):
            out.extend(_expand_asset_leaf(path, str(desc)))
    return out


def _normalize_binary_assets(leaves: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """Rewrite asset targets the text-only code generator can't emit as valid binary.

    ``favicon.ico`` -> ``favicon.svg`` (SVG is text). When a favicon becomes SVG, the ``index.html``
    spec is augmented so the generated markup links it as ``image/svg+xml``.
    """
    has_svg_favicon = False
    rewritten: list[tuple[str, Any]] = []
    for path, desc in leaves:
        low = path.lower()
        if low.endswith("favicon.ico"):
            path = path[: -len("favicon.ico")] + "favicon.svg"
            desc = "Site favicon as SVG (image/svg+xml)."
            has_svg_favicon = True
        elif low.endswith("favicon.svg"):
            has_svg_favicon = True
        rewritten.append((path, desc))
    if not has_svg_favicon:
        return rewritten
    out: list[tuple[str, Any]] = []
    for path, desc in rewritten:
        if _basename(path) == "index.html":
            desc = str(desc).rstrip() + ' Link the favicon with <link rel="icon" type="image/svg+xml" href="/favicon.svg"> in <head>.'
        out.append((path, desc))
    return out


def _source_leaves(tree: dict, *, harness_covers: bool = True) -> list[tuple[str, Any]]:
    """File leaves to generate for a structure tree.

    Excludes test files, directory entries, and the test-harness configs the deterministic scaffold
    already renders (see :data:`_SCAFFOLD_OWNED_RE`) — EXCEPT bulk ``assets/…/`` folders, which are
    expanded into concrete ``.svg`` targets (see :func:`_asset_leaves`) so the assets they stand
    for are actually generated. Binary targets the generator can't emit are normalized to SVG
    (see :func:`_normalize_binary_assets`). Centralizing all of it here keeps the builders,
    ``_reconcile_uncovered`` and ``_missing_after_reconcile`` in agreement — and, for the
    scaffold-owned files, keeps the "every leaf is produced" guarantee honest: they ARE produced,
    just by ``scaffold_node`` rather than by a work item, so counting them as uncovered would be
    the false alarm and generating them would be the real bug.

    ``harness_covers`` (default ``True`` = today's behaviour) should be the caller's
    ``_harness_covered_sides`` result for THIS tree's side: when the scaffold will NOT actually
    render a harness here (``False``), a scaffold-owned leaf is left in rather than dropped — an
    unproduced leaf is a worse failure (never generated at all) than letting a work item generate
    its own best-effort version.
    """
    leaves: list[tuple[str, Any]] = [
        (p, d)
        for p, d in _walk(tree)
        if not _is_dir_leaf(p) and not _is_test_path(p) and not (_is_scaffold_owned(p) and harness_covers)
    ]
    leaves += _asset_leaves(tree)
    return _normalize_binary_assets(leaves)


# -- canonical two-folder re-rooting ---------------------------------------
#
# Design packs wrap their structure trees inconsistently: ``root/backend/…`` (resources),
# ``auth-backend/…`` / ``quickbite-backend/…`` (per-side project dirs), or a bare ``src/…`` (no
# wrapper at all). To make the GENERATED project always land in two clean top-level folders, every
# backend leaf is re-rooted under ``backend/`` and every frontend leaf under ``frontend/``. Applied
# ONCE in build_plan's adaptive path (before any builder runs) so target files, the reconcile sweep
# and the completeness check all agree on the canonical paths. The legacy flat-CSV path keeps its
# byte-for-byte output and is left un-rerooted.

#: Generic wrapper directories a whole project may be nested under; peeled off when re-rooting.
#: Deliberately EXCLUDES source/package roots — ``src``, ``source``, and ``app``. ``app`` is this
#: project's own FastAPI convention (``app/main.py``, ``from app.services import …``); peeling it
#: would collapse ``app/main.py`` → ``backend/main.py`` and break the ``from app.…`` imports the
#: generator emits, so ``app`` is treated as a real package dir, never a wrapper. Do not add
#: ``app``/``src``/``source`` here.
_GENERIC_WRAPPERS = {"root", "project", "workspace", "repo"}

#: Extensionless files that are legitimately part of a tree (kept despite having no ``.``); anything
#: else with no extension (e.g. a bare ``notes`` annotation some packs put beside the tree) is
#: treated as metadata, not a file, and dropped.
_KNOWN_EXTENSIONLESS_FILES = {
    "dockerfile", "makefile", "procfile", "license", "readme", "codeowners",
    ".gitignore", ".dockerignore", ".gitattributes", ".npmrc", ".nvmrc",
}


def _looks_like_file(path: str) -> bool:
    """Whether a leaf names a real file rather than a metadata annotation (a bare ``notes`` key some
    packs put beside the tree). A real file has an extension or is a known extensionless file."""
    base = _basename(path)
    return "." in base or base in _KNOWN_EXTENSIONLESS_FILES


def _reroot(path: str, side: str) -> str:
    """Re-root one leaf path under a canonical top-level ``side`` dir ('backend'/'frontend').

    Peels leading wrapper segments — generic (``root/``, ``app/``) or side-named (``backend/``,
    ``auth-backend/``, ``quickbite-backend/``) — then anchors the remainder under ``<side>/``. So
    ``root/backend/src/app.js`` → ``backend/src/app.js`` and a bare ``src/app.js`` →
    ``backend/src/app.js`` collapse to the same layout."""
    side_suffixes = (f"-{side}", f"_{side}")
    segs = [s for s in path.split("/") if s]
    while segs:
        head = segs[0].lower()
        if head in _GENERIC_WRAPPERS or head == side or head.endswith(side_suffixes):
            segs = segs[1:]
        else:
            break
    return "/".join([side, *segs]) if segs else side


_INITIAL_MIGRATION_SPEC = (
    "Initial Alembic migration (revision \"0001\", down_revision None). upgrade() creates EVERY table "
    "of the design schema exactly as specified — columns, types, nullability, server defaults, primary "
    "keys, unique constraints, CHECK constraints, indexes (including partial ones) and foreign keys — "
    "in dependency order; downgrade() drops them in reverse order. Use the same table and column names "
    "as the ORM models."
)


def _synthesized_dir_leaves(tree: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """``(files a directory leaf stands for, directory leaves left unexpanded)``.

    Structure trees list some folders as one directory entry with a description instead of their
    files. Asset folders are expanded elsewhere (:func:`_asset_leaves`); everything else used to be
    dropped SILENTLY — so an Alembic ``migrations/versions/`` folder produced no migration at all
    and the deployed app ran ``alembic upgrade head`` against nothing, creating no tables.

    A ``versions/`` folder next to an Alembic ``env.py`` (or in a tree with ``alembic.ini``) becomes
    an initial migration built from the full schema. Any other directory leaf is returned in the
    second list, so the caller reports it instead of losing it.
    """
    leaves = _walk(tree)
    files = {p for p, _ in leaves if not _is_dir_leaf(p)}
    has_alembic = any(_basename(p) == "alembic.ini" for p in files)
    synthesized: list[tuple[str, str]] = []
    dropped: list[str] = []
    for path, desc in leaves:
        if not _is_dir_leaf(path) or _is_asset_dir_leaf(path):
            continue
        parent = _dir_of(path.rstrip("/"))
        if _basename(path) == "versions" and (f"{parent}env.py" in files or has_alembic):
            synthesized.append((f"{path}0001_initial.py", _INITIAL_MIGRATION_SPEC))
        else:
            dropped.append(f"{path} ({str(desc)[:80]})")
    return synthesized, dropped


def _rerooted_source_leaves(tree: dict, side: str, *, harness_covers: bool = True) -> list[tuple[str, Any]]:
    """Like :func:`_source_leaves` but re-rooted under a single canonical ``side/`` folder
    ('backend'/'frontend'), filtering out non-file metadata leaves (e.g. a bare ``notes`` key).
    Re-rooting happens AFTER asset synthesis + favicon normalization, so those transformations
    are preserved. Dropped (extensionless, non-known) leaves are logged at DEBUG so a later "why
    wasn't this file generated?" is easy to trace — if a real extensionless file (e.g. ``bin/serve``)
    shows up here, add it to :data:`_KNOWN_EXTENSIONLESS_FILES`.

    ``harness_covers`` passes straight through to :func:`_source_leaves` — see there.

    Also adds the files a DIRECTORY leaf stands for when its meaning is unambiguous (see
    :func:`_synthesized_dir_leaves`) — adaptive path only, so the legacy builders' output is unchanged."""
    out: list[tuple[str, Any]] = []
    for path, desc in _source_leaves(tree, harness_covers=harness_covers) + _synthesized_dir_leaves(tree)[0]:
        if _looks_like_file(path):
            out.append((_reroot(path, side), desc))
        else:
            logger.debug("plan_builder: skipping non-file leaf %r in %s tree (treated as metadata)", path, side)
    return out


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


# -- generation order -------------------------------------------------------
#
# Each work item is generated in its own LLM call and sees only the PATHS of what earlier items
# produced. An item generated before the code it imports therefore has to guess those imports — a
# FastAPI entry point generated FIRST (it was simply the first directory in the tree) imported a
# package `app.routers` that never existed, and the app could not start. So items are ordered
# dependencies-first by LAYER, inferred from generic path / name / description hints (the same
# data-driven approach as the role hints above): configuration and utilities, then the data model,
# repositories, services, middleware, feature modules, wiring — and the entry point(s) and project
# manifests last, when everything they reference exists. A directory mixing layers (an app root
# holding config.py, dependencies.py AND main.py) is split so each part lands in its own layer.

_BACKEND_LAYER_RANK = {
    "config": 0, "utils": 10, "models": 20, "schemas": 30, "repositories": 40, "other": 45,
    "services": 50, "middleware": 55, "modules": 60, "wiring": 70, "migrations": 75, "entry": 80,
    "manifest": 90,
}
_FRONTEND_LAYER_RANK = {
    "assets": 0, "types": 10, "utils": 20, "other": 25, "services": 30, "state": 35, "hooks": 40,
    "components-ui": 45, "components": 50, "pages": 60, "router": 70, "entry": 80, "manifest": 90,
}

#: Descriptions that say "this file starts / assembles the application".
_ENTRY_DESC_RE = re.compile(
    r"\b(?:application factory|app factory|entry[ -]?point|bootstraps?\b|starts? the (?:http |web |api )?server|"
    r"root (?:application )?component|createRoot|ReactDOM|renders? (?:the )?<?App\b)",
    re.IGNORECASE,
)
#: Descriptions of files that wire many modules together (a router aggregator).
_WIRING_DESC_RE = re.compile(
    r"\b(?:registers|mounts|includes|combines|aggregates) (?:all|every|each) (?:of the )?"
    r"(?:routers?|routes?|blueprints|modules|reducers|controllers)\b",
    re.IGNORECASE,
)
_BACKEND_ENTRY_FILES = {
    "main.py", "app.py", "server.py", "wsgi.py", "asgi.py", "manage.py",
    "server.js", "server.ts", "app.js", "app.ts", "main.js", "main.ts", "index.js", "index.ts",
}
_FRONTEND_ENTRY_FILES = {
    "main.tsx", "main.ts", "main.jsx", "main.js", "index.tsx", "index.jsx", "index.js",
    "app.tsx", "app.jsx", "app.js", "app.vue", "main.vue",
}
_MANIFEST_FILES = {
    "requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py", "setup.cfg", "pipfile",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml", "alembic.ini", "package.json",
    "package-lock.json", "nodemon.json", ".env.example", ".env.sample", "makefile", "procfile",
    ".dockerignore", ".gitignore", "readme.md", "index.html", ".eslintrc", ".eslintrc.js",
    ".eslintrc.cjs", ".eslintrc.json", ".prettierrc", ".prettierrc.json",
}
_MANIFEST_PREFIXES = ("tsconfig", "vite.config", "webpack.config", "eslint.config", "postcss.config",
                      "tailwind.config", "jest.config", "vitest.config", "babel.config")


def _layer_parts(path: str) -> tuple[list[str], str, str, set[str]]:
    """(segments after the side folder, basename, stem, directory names) — all lowercased."""
    segs = [s.lower() for s in path.split("/")[1:] if s]
    base = segs[-1] if segs else ""
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return segs, base, stem, set(segs[:-1])


def _is_manifest(base: str, segs: list[str]) -> bool:
    return base in _MANIFEST_FILES or (len(segs) <= 2 and base.startswith(_MANIFEST_PREFIXES))


def _backend_layer(path: str, desc: str) -> str:
    segs, base, stem, dirs = _layer_parts(path)
    if _is_manifest(base, segs):
        return "manifest"
    if (base in _BACKEND_ENTRY_FILES and len(segs) <= 2) or _ENTRY_DESC_RE.search(desc):
        return "entry"
    if dirs & {"migrations", "alembic", "versions", "seeds", "seeders"}:
        return "migrations"
    if _WIRING_DESC_RE.search(desc):
        return "wiring"
    if dirs & {"config", "configs", "settings"} or stem in {
        "config", "settings", "configuration", "constants", "database", "db", "session",
        "extensions", "env", "logging_config",
    }:
        return "config"
    if dirs & {"utils", "util", "helpers", "helper", "lib", "common", "shared"} or stem.startswith(("util", "helper")):
        return "utils"
    if _is_model(path):
        return "models"
    if dirs & {"schemas", "dto", "dtos", "serializers", "validators"}:
        return "schemas"
    if dirs & {"repositories", "repository", "repos", "dao", "daos"} or "repositor" in stem or stem.endswith("_repo"):
        return "repositories"
    if dirs & {"db", "database", "persistence"}:
        return "config"     # the db client/session (its migrations/seeds/repositories are matched above)
    if dirs & {"services", "usecases", "integrations", "providers", "adapters", "clients"}:
        return "services"
    if dirs & {"middleware", "middlewares", "guards", "interceptors", "decorators", "dependencies", "deps"} or stem in {
        "dependencies", "deps", "security", "middleware", "auth_middleware",
    }:
        return "middleware"
    if (dirs & {"modules", "routes", "routers", "controllers", "api", "endpoints", "views", "handlers"}
            or _is_handler(path) or _is_service(path) or _is_schema_file(path)):
        return "modules"
    if len(segs) == 1 or "config" in stem:
        # A loose file at the side's root that is neither entry nor manifest is tool configuration
        # (knexfile.js, ormconfig.ts, gunicorn.conf.py): it is read by others, so it goes first.
        return "config"
    return "other"


def _frontend_layer(path: str, desc: str) -> str:
    segs, base, stem, dirs = _layer_parts(path)
    original_base = path.rsplit("/", 1)[-1]
    if _is_manifest(base, segs):
        return "manifest"
    if (base in _FRONTEND_ENTRY_FILES and len(segs) <= 2) or _ENTRY_DESC_RE.search(desc):
        return "entry"
    if dirs & {"router", "routes", "routing", "navigation"} or stem in {"approuter", "router", "routes"}:
        return "router"
    if dirs & {"styles", "style", "assets", "public", "theme", "themes", "fonts", "icons", "images", "css"} or base.endswith(
        (".css", ".scss", ".sass", ".less", ".svg")
    ):
        return "assets"
    if dirs & {"types", "interfaces", "typings", "@types", "models"} or base.endswith(".d.ts"):
        return "types"
    if dirs & {"utils", "util", "helpers", "lib", "constants", "config"}:
        return "utils"
    if dirs & {"services", "api", "apis", "clients", "http"}:
        return "services"
    if dirs & {"context", "contexts", "store", "stores", "state", "providers", "redux", "slices", "atoms"}:
        return "state"
    if dirs & {"hooks", "composables"} or re.match(r"use[A-Z]", original_base):
        return "hooks"
    if dirs & {"pages", "views", "screens"}:
        return "pages"
    if dirs & {"components", "widgets", "layouts", "layout", "ui"}:
        return "components-ui" if "ui" in dirs else "components"
    return "other"


def _is_package_marker(path: str) -> bool:
    """A file that exists to make its directory a package — it belongs with its siblings' layer."""
    return _basename(path) == "__init__.py"


def _file_table_stem(path: str) -> str:
    """Entity stem a model/repository FILE is about: ``vehicle_type_repository.py`` -> ``vehicletype``."""
    stem = _model_stem(path)
    for suffix in ("repository", "repo", "dao", "model", "entity", "schema"):
        if stem.endswith(suffix) and len(stem) > len(suffix):
            return _singular(stem[: -len(suffix)])     # users.repository.js -> "user"
    return stem


def _order_and_shape(items: list[WorkItem], side: str, entities: list[str] | None = None) -> list[WorkItem]:
    """Split mixed-layer directory items, move entry points into one final ``<side>-entry`` item,
    give data-layer items their tables, and order everything dependencies-first (stable within a
    layer, so the tree's own order still breaks ties). See the section comment above."""
    layer_of = _backend_layer if side == "backend" else _frontend_layer
    ranks = _BACKEND_LAYER_RANK if side == "backend" else _FRONTEND_LAYER_RANK
    entities = entities or []
    used_ids = {item.id for item in items}
    shaped: list[tuple[int, int, WorkItem]] = []
    entry_files: list[str] = []
    entry_specs: dict[str, str] = {}

    for pos, item in enumerate(items):
        layers = {f: layer_of(f, item.file_specs.get(f, "")) for f in item.target_files}
        # A feature module (a directory holding a router/controller) is ONE unit — its router,
        # service and schemas are generated together so they agree on each other's names.
        # (Judged by computed layer, not by name: `middleware/error_handlers.py` contains "handler"
        # but is middleware, and must not drag its directory after the modules that use it.)
        if side == "backend" and any(lyr == "modules" for lyr in layers.values()):
            layers = {f: (lyr if lyr in ("entry", "manifest", "migrations", "wiring") else "modules")
                      for f, lyr in layers.items()}
        # A package marker (__init__.py) goes with the EARLIEST layer of its directory.
        others = [lyr for f, lyr in layers.items() if not _is_package_marker(f) and lyr != "entry"]
        if others:
            earliest = min(others, key=lambda lyr: ranks[lyr])
            layers = {f: (earliest if _is_package_marker(f) else lyr) for f, lyr in layers.items()}
        for f, lyr in layers.items():
            if lyr == "entry":
                entry_files.append(f)
                entry_specs[f] = item.file_specs.get(f, "")
        bands = sorted({lyr for lyr in layers.values() if lyr != "entry"}, key=lambda lyr: ranks[lyr])
        for i, band in enumerate(bands):
            files = [f for f in item.target_files if layers[f] == band]
            update: dict[str, Any] = {
                "target_files": files,
                "file_specs": {f: item.file_specs[f] for f in files if f in item.file_specs},
            }
            if i > 0:
                new_id, n = f"{item.id}-{band}", 1
                while new_id in used_ids:
                    n += 1
                    new_id = f"{item.id}-{band}-{n}"
                used_ids.add(new_id)
                # Endpoint/screen/requirement metadata describes the directory's main part (the
                # first band); a split-off part carries none rather than a misleading copy.
                update.update({"id": new_id, "endpoints": [], "tables": [], "screens": [], "requirement_ids": []})
            if side == "backend" and band in ("models", "repositories", "migrations") and entities:
                # The data layer is exactly where the schema matters most, yet these items carried
                # tables=[] (tables were attached only via endpoints). Match each file to its table;
                # a file matching none (an __init__ re-exporting everything, a migration) gets all.
                matched = {t for f in files for t in entities if _table_stem(t) == _file_table_stem(f)}
                update["tables"] = sorted(matched if (matched and band != "migrations") else entities)
            shaped.append((ranks[band], pos, item.model_copy(update=update)))

    if entry_files:
        entry_id, n = f"{side}-entry", 1
        while entry_id in used_ids:
            n += 1
            entry_id = f"{side}-entry-{n}"
        shaped.append((ranks["entry"], len(items), WorkItem(id=entry_id, target_files=entry_files, file_specs=entry_specs)))
    shaped.sort(key=lambda t: (t[0], t[1]))
    return [item for _rank, _pos, item in shaped]


def _tables_for_endpoint(path: str, operation_id: str, entities: list[str]) -> list[str]:
    """Tables an operation is about: those whose stem EQUALS a literal path segment, and only when
    the path names none, those named by an operationId word (``getCurrentUser`` -> users).

    The substring match it replaces (still used, unchanged, by the legacy builders) over-matched:
    ``/vehicle-types`` also "touched" ``vehicles`` because "vehicle" is inside "vehicletypes"."""
    segments = {_singular(_norm(s)) for s in path.split("/") if s and not s.startswith("{")}
    by_path = [t for t in entities if _table_stem(t) in segments]
    if by_path:
        return by_path
    words = {_singular(w.lower()) for w in re.findall(r"[A-Z]?[a-z0-9]+", operation_id or "")}
    return [t for t in entities if _table_stem(t) in words]


def _backend_items_adaptive(
    resolved: "design_pack.ResolvedPack", *, harness_covers: bool = True
) -> list[WorkItem]:
    leaves = _rerooted_source_leaves(resolved.backend_tree, "backend", harness_covers=harness_covers)
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
        tables_by_dir[module_dir].update(_tables_for_endpoint(ep["path"], op, entities))
        reqs_by_dir[module_dir].update(resolved.tag_reqs.get(ep.get("tag", ""), []))

    items = _items_from_groups(
        "backend",
        leaves,
        endpoints_by_dir=endpoints_by_dir,
        tables_by_dir=tables_by_dir,
        reqs_by_dir=reqs_by_dir,
    )
    return _order_and_shape(items, "backend", entities)


def _frontend_items_adaptive(
    resolved: "design_pack.ResolvedPack", *, harness_covers: bool = True
) -> list[WorkItem]:
    leaves = _rerooted_source_leaves(resolved.frontend_tree, "frontend", harness_covers=harness_covers)
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

    items = _items_from_groups(
        "frontend",
        leaves,
        screens_by_dir=screens_by_dir,
        reqs_by_dir=reqs_by_dir,
    )
    return _order_and_shape(items, "frontend")


def _reconcile_uncovered(
    backend_tree: dict, frontend_tree: dict, items: list[WorkItem], *,
    reroot: bool = False, backend_covers: bool = True, frontend_covers: bool = True,
) -> list[WorkItem]:
    """Sweep any structure-tree file not already covered by an item into a catch-all item.

    Shared by both builders. For the adaptive builders this is normally a no-op (they emit one
    item per directory, covering every leaf) — a guard against a future filtering change silently
    dropping files. For the LEGACY (per-operation/per-screen) builders it does real work: those
    only ever target files tied to a SPECIFIC operation/screen, so shared/cross-cutting
    infrastructure (main.py, config/settings.py, core/security.py, a frontend App.tsx, api/
    client.ts, ...) is never assigned to any item on its own — this is what sweeps those in.

    ``reroot`` re-roots swept leaves under canonical ``backend/`` / ``frontend/`` folders, matching
    what the adaptive builders emit. The legacy path leaves it off to keep its byte-for-byte paths.
    ``backend_covers``/``frontend_covers`` (see :func:`_harness_covered_sides`) must match what the
    builders used for the SAME side, or a scaffold-owned leaf could be swept in here as a catch-all
    item even though the builder above already (correctly, or incorrectly) excluded it.
    """
    leaves_of = (
        (lambda tree, side, covers: _rerooted_source_leaves(tree, side, harness_covers=covers))
        if reroot else
        (lambda tree, _side, covers: _source_leaves(tree, harness_covers=covers))
    )
    covered = {f for item in items for f in item.target_files}
    used_ids = {item.id for item in items}
    extra: list[WorkItem] = []
    for prefix, tree, covers in (
        ("backend", backend_tree, backend_covers), ("frontend", frontend_tree, frontend_covers),
    ):
        uncovered = [(p, d) for p, d in leaves_of(tree, prefix, covers) if p not in covered]
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
