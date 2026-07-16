"""Deterministic implementation-plan builder (no LLM).

Reads a design pack's ``api-mapping.csv`` + ``backend-structure.json`` +
``frontend-structure.json`` (+ ``schema.sql`` for table inference) and emits a list of
:class:`~app.models.work_item.WorkItem`, grouped by (screen, layer):

* one BACKEND item per operationId — controller + service + DTO + entity(≈repository)
  target files (from backend-structure.json), covering its endpoint + req_ids + the tables it
  touches (inferred from schema.sql);
* one FRONTEND item per screen — page + api-module target files (from frontend-structure.json),
  covering its route + req_ids + screen name.

Run ``python -m app.services.plan_builder`` to (re)generate
``app/tests/fixtures/implementation-plan.ecommerce.json``.
"""

from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from typing import Any

from app.models import WorkItem

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


def _read_csv(pack: Path) -> list[dict[str, str]]:
    with (pack / "api-mapping.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _load_json(pack: Path, name: str) -> dict:
    return json.loads((pack / name).read_text(encoding="utf-8"))


def _canonical_endpoint(method: str, path: str) -> str:
    return f"{method.strip().upper()} {path.split('?', 1)[0].strip()}"


# --------------------------------------------------------------------------- builders

def build_plan(pack_dir: str | Path) -> list[WorkItem]:
    """Build the deterministic implementation plan for a design pack."""
    pack = Path(pack_dir)
    rows = _read_csv(pack)
    tables = _table_names((pack / "schema.sql").read_text(encoding="utf-8"))
    backend = _load_json(pack, "backend-structure.json")
    frontend = _load_json(pack, "frontend-structure.json")

    return _backend_items(rows, tables, backend) + _frontend_items(rows, frontend)


def _backend_items(rows: list[dict[str, str]], tables: list[str], backend: dict) -> list[WorkItem]:
    leaves = _walk(backend.get("tree", {}))
    controllers = {p: str(v) for p, v in leaves if p.endswith(".controller.ts")}
    services = [p for p, _ in leaves if p.endswith(".service.ts")]
    dto_dirs = [p for p, _ in leaves if p.endswith("dto/")]
    entities = [p for p, _ in leaves if p.endswith(".entity.ts")]

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

        module_dir = _module_for_op(op, controllers)
        target_files = _backend_targets(op, module_dir, controllers, services, dto_dirs, entities, touched)

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


def _module_for_op(op: str, controllers: dict[str, str]) -> str:
    """The module dir (e.g. 'src/auth/') whose controller description names this operationId."""
    for path, desc in controllers.items():
        if op in desc:
            return path.rsplit("/", 1)[0] + "/"
    return ""


def _backend_targets(
    op: str,
    module_dir: str,
    controllers: dict[str, str],
    services: list[str],
    dto_dirs: list[str],
    entities: list[str],
    touched: list[str],
) -> list[str]:
    files: list[str] = []
    if module_dir:
        files += [p for p in controllers if p.startswith(module_dir)]
        files += [p for p in services if p.startswith(module_dir)]
        dto = next((d for d in dto_dirs if d.startswith(module_dir)), None)
        if dto:
            files.append(f"{dto}{op[:1].upper()}{op[1:]}Dto.ts")
    # repository ≈ entity: pick entity files whose stem matches an inferred table
    wanted = {_table_stem(t) for t in touched}
    files += [e for e in entities if _entity_stem(e) in wanted]
    # de-dup, preserve order
    return list(dict.fromkeys(files)) or [f"{module_dir}{op}.ts"]


def _entity_stem(entity_path: str) -> str:
    base = entity_path.rsplit("/", 1)[-1].replace(".entity.ts", "")
    norm = _norm(base)
    return norm[:-1] if norm.endswith("s") else norm


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
