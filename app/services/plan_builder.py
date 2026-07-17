"""Deterministic implementation-plan builder (no LLM).

Artifacts are identified by ROLE from their content — never by fixed filename — via
:mod:`app.services.design_pack`, so the API surface may arrive as OpenAPI or a flat CSV, the
schema as SQL DDL or JSON/Mongo, etc. From that normalized view it emits a list of
:class:`~app.models.work_item.WorkItem`, grouped by (screen, layer):

* one BACKEND item per operationId — handler + service + schema/DTO + model(≈entity)
  target files (located in the backend structure tree), covering its endpoint + req_ids + the
  tables it touches (inferred from the schema, SQL or JSON);
* one FRONTEND item per screen — page + api-module target files (located in the frontend
  structure tree), covering its route + req_ids + screen name.

Legacy self-contained flat-CSV packs keep their original byte-for-byte output; every other
shape goes through the adaptive builders.

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
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from app.models import WorkItem
from app.services import design_pack

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


# --------------------------------------------------------------------------- builders

def build_plan(pack_dir: str | Path) -> list[WorkItem]:
    """Build the deterministic implementation plan for a design pack.

    Artifacts are identified by ROLE from their content (see :mod:`app.services.design_pack`),
    so filenames and formats can vary between design hand-offs. Two shapes are supported:

    * the legacy flat mapping (a CSV carrying its own operation_id/req_ids/endpoint_path columns)
      → the original stack-agnostic builders, unchanged;
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
        return _backend_items(rows, tables, backend) + _frontend_items(rows, frontend)

    # Adaptive path: normalize whatever formats arrived, then build from the neutral view.
    resolved = design_pack.resolve(pack, roles)
    if resolved is None or (not resolved.endpoints and not resolved.screens):
        raise FileNotFoundError(
            f"{pack_dir}: no recognizable design-package artifacts (need an OpenAPI spec or a "
            "UI↔API mapping table)."
        )
    return _backend_items_adaptive(resolved) + _frontend_items_adaptive(resolved)


def _structure_obj(roles: dict, role: str) -> dict:
    """Full structure JSON (with its ``tree`` wrapper) for the legacy builders, or ``{}``."""
    df = design_pack._first(roles, role)
    return df.obj if df and isinstance(df.obj, dict) else {}


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


def _frontend_items(rows: list[dict[str, str]], frontend: dict) -> list[WorkItem]:
    leaves = _walk(frontend.get("tree", {}))
    page_by_route = {
        str(desc).replace("route", "").strip(): path
        for path, desc in leaves
        if "/pages/" in path and path.endswith(".tsx") and str(desc).startswith("route ")
    }
    api_files = [(path, str(desc)) for path, desc in leaves if "/api/" in path and path.endswith(".ts")]

    items: list[WorkItem] = []
    seen: set[str] = set()
    for row in rows:
        route_id = row.get("route_id", "").strip()
        screen = row.get("screen", "").strip()
        if not route_id or route_id in seen or route_id not in page_by_route:
            continue  # skip globals / logout / anything without a real page
        seen.add(route_id)

        screen_rows = [r for r in rows if r.get("route_id", "").strip() == route_id]
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
            if sn and (sn == stem or stem in sn or sn in stem):
                candidate = "/".join(segs[: i + 1]) + "/"
                if len(candidate) > len(best):
                    best = candidate
    return best


def _singular(norm: str) -> str:
    return norm[:-1] if norm.endswith("s") and len(norm) > 1 else norm


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-") or "screen"


def _backend_items_adaptive(resolved: "design_pack.ResolvedPack") -> list[WorkItem]:
    entities = resolved.entities
    leaves = _walk(resolved.backend_tree)
    handlers = [p for p, _ in leaves if _is_handler(p) and not _is_test_path(p)]
    services = [p for p, _ in leaves if _is_service(p) and not _is_test_path(p)]
    schemas = [p for p, _ in leaves if (_is_schema_file(p) or _is_validator(p)) and not _is_test_path(p)]
    models = [p for p, _ in leaves if _is_model(p) and not _is_test_path(p)]
    default_ext = _dominant_ext([p for p, _ in leaves]) or ".js"
    module_search = handlers + services

    items: list[WorkItem] = []
    seen: set[str] = set()
    for ep in resolved.endpoints:
        op = ep.get("operation_id") or _synth_op(ep["method"], ep["path"])
        if op in seen:
            continue
        seen.add(op)

        endpoint = _canonical_endpoint(ep["method"], ep["path"])
        touched = _tables_touched(ep["path"], op, entities)
        req_ids = resolved.tag_reqs.get(ep.get("tag", ""), [])
        module_dir = _module_dir_for_tag(ep.get("tag", ""), module_search)
        if not module_dir:
            first_seg = re.sub(r"\{[^}]+\}", "", ep["path"]).strip("/").split("/")
            module_dir = _module_dir_for_tag(first_seg[0] if first_seg and first_seg[0] else "", module_search)

        targets = _adaptive_backend_targets(module_dir, handlers, services, schemas, models, touched, default_ext, op)
        items.append(
            WorkItem(
                id=f"backend-{op}",
                requirement_ids=req_ids,
                endpoints=[endpoint],
                tables=touched,
                screens=[],
                target_files=targets,
            )
        )
    return items


def _adaptive_backend_targets(
    module_dir: str,
    handlers: list[str],
    services: list[str],
    schemas: list[str],
    models: list[str],
    touched: list[str],
    default_ext: str,
    op: str,
) -> list[str]:
    files: list[str] = []
    if module_dir:
        files += [p for p in handlers if p.startswith(module_dir)]
        files += [p for p in services if p.startswith(module_dir)]
        files += [p for p in schemas if p.startswith(module_dir)]
    wanted = {_table_stem(t) for t in touched}
    files += [m for m in models if _model_stem(m) in wanted]
    files = list(dict.fromkeys(files))
    if files:
        return files
    ext = _dominant_ext([p for p in handlers + services if p.startswith(module_dir)]) or default_ext
    return [f"{module_dir}{op}{ext}"]


def _frontend_items_adaptive(resolved: "design_pack.ResolvedPack") -> list[WorkItem]:
    leaves = _walk(resolved.frontend_tree)
    pages = [p for p, _ in leaves if _is_page(p)]
    services = [(p, str(v)) for p, v in leaves if _is_api_service(p)]
    ep_index = resolved.endpoint_by_key()

    items: list[WorkItem] = []
    seen: set[str] = set()
    for screen in resolved.screens:
        name = screen["name"]
        sid = _slug(name)
        if sid in seen:
            continue
        keys = screen.get("endpoints", [])
        tags = {ep_index.get(k, {}).get("tag", "") for k in keys}
        req_ids = sorted({r for tag in tags for r in resolved.tag_reqs.get(tag, [])})

        target_files: list[str] = []
        page = _match_page(name, pages)
        if page:
            target_files.append(page)
        target_files += _match_services(services, keys, ep_index)
        target_files = list(dict.fromkeys(target_files))
        if not target_files:
            continue
        seen.add(sid)
        items.append(
            WorkItem(
                id=f"frontend-{sid}",
                requirement_ids=req_ids,
                endpoints=[],
                tables=[],
                screens=[name],
                target_files=target_files,
            )
        )
    return items


def _match_page(screen_name: str, pages: list[str]) -> str:
    """Best page component for a screen by token subset (e.g. 'Customer Login' → 'LoginPage.jsx')."""
    from app.services.design_pack import _tokens

    stoks = _tokens(screen_name) - {"page", "screen", "view"}
    best, best_score = "", 0
    for p in pages:
        stem = _basename(p).rsplit(".", 1)[0]
        ptoks = _tokens(stem) - {"page", "screen", "view", "module", "test"}
        if not ptoks or not ptoks <= stoks:
            continue
        score = len(ptoks & stoks) + (1 if "page" in _basename(p).lower() else 0)
        if score > best_score:
            best, best_score = p, score
    return best


def _match_services(services: list[tuple[str, str]], keys: list[str], ep_index: dict) -> list[str]:
    """API-module files a screen depends on.

    Matched two robust ways (not by prose): the endpoint's first path segment appearing as a
    literal ``/segment`` in the module's description (modules list the routes they call), or the
    segment matching the module filename (``auth`` → ``auth.service.js``). Avoids false hits like
    ``apiClient.js`` matching every auth screen because its blurb mentions "Authorization".
    """
    from app.services.design_pack import _singular as _sing, _norm as _n

    seg0s: set[str] = set()
    for k in keys:
        path = ep_index.get(k, {}).get("path") or k.split(" ", 1)[-1]
        segs = [s for s in re.sub(r"\{[^}]+\}", "", path).strip("/").split("/") if s]
        if segs:
            seg0s.add(segs[0])
    out: list[str] = []
    for path, desc in services:
        base = _n(_basename(path))
        desc_l = desc.lower()
        if any(f"/{seg}" in desc_l or _sing(_n(seg)) in base for seg in seg0s):
            out.append(path)
    return list(dict.fromkeys(out))


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
