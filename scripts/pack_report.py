"""Read-only report: how much of a design pack actually reaches code generation.

Runs the same deterministic stages a real run starts with — role detection, ``build_plan``, the
naming contract, and ``CodeGeneratorAgent._assemble_context`` for every work item — and prints
what each stage made of the pack. No LLM, no executor, nothing written unless ``--json`` or
``--dump-contexts`` is passed.

It exists because "the generated app doesn't fit together" is usually decided BEFORE the model is
called: an artifact the loader can't find, a contract with no entities, a backend item whose prompt
carries no schema. Those are invisible in a 3-6 hour run and obvious here in a second, so this is
the before/after measurement for every change to how the pack is read.

Usage (from SDLC_Implementation/):

    ./.venv/Scripts/python.exe scripts/pack_report.py uploads/v1
    ./.venv/Scripts/python.exe scripts/pack_report.py uploads/v1 fixtures/resources --json out.json
    ./.venv/Scripts/python.exe scripts/pack_report.py uploads/v1 --dump-contexts ctx/   # one file per item
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so `app.*` imports work

from app.agents.code_generator import CodeGeneratorAgent  # noqa: E402
from app.services import design_pack  # noqa: E402
from app.services.boilerplate import render_scaffold  # noqa: E402
from app.services.naming_contract import build_naming_contract_from_package  # noqa: E402
from app.services.pack_index import PackIndex  # noqa: E402
from app.services.plan_builder import _load_pack_artifacts, build_plan  # noqa: E402

#: Requirement-like ids anywhere in the requirements artifact (REQ-1, NFR-2, BR-1, CON-1 ...).
_REQ_ID_RE = re.compile(r"^[A-Z]{2,5}-\d+$")

#: Files that start an app. Position matters: an entry point generated before the modules it
#: imports has nothing real to import from, which is how ``main.py`` came to import ``app.routers``.
_BACKEND_ENTRY_NAMES = {"main.py", "app.py", "server.py", "server.js", "server.ts", "app.js", "app.ts"}
_FRONTEND_ENTRY_NAMES = {"main.tsx", "main.ts", "main.jsx", "main.js", "app.tsx", "app.jsx", "index.tsx"}

#: Markers that an API section carries real operation detail, not just bare path names.
_API_DETAIL_MARKERS = ("requestBody", "responses", "$ref", "operationId")


class _WarningCollector(logging.Handler):
    """Capture the pipeline's own warnings (unmapped tags, dropped leaves ...) for the report."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _requirement_ids(requirements: Any) -> dict[str, str]:
    """``id -> text`` for every requirement-like object in the requirements artifact, whatever
    section it sits in (functional, non_functional, business_rules ...)."""
    found: dict[str, str] = {}

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            rid = node.get("id")
            if isinstance(rid, str) and _REQ_ID_RE.match(rid.strip()):
                found.setdefault(rid.strip(), str(node.get("text") or node.get("description") or ""))
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(requirements)
    return found


def _section(text: str, heading: str) -> str:
    """Body of one ``## <heading>`` section of an assembled context ("" if absent)."""
    start = text.find(f"## {heading}")
    if start < 0:
        return ""
    nxt = text.find("\n## ", start + 3)
    return text[start: nxt if nxt >= 0 else len(text)]


def _side(item_id: str) -> str:
    return "backend" if item_id.startswith("backend") else "frontend" if item_id.startswith("frontend") else "other"


def _entry_positions(items: list[Any], side: str, names: set[str]) -> dict[str, Any]:
    """Where entry-point files sit in the generation order of their side."""
    side_items = [w for w in items if _side(w.id) == side]
    hits = []
    for pos, item in enumerate(side_items):
        for path in item.target_files:
            if Path(path).name.lower() in names:
                hits.append({"file": path, "item": item.id, "position": pos, "of": len(side_items)})
    return {"entries": hits, "all_last": bool(hits) and all(h["position"] == len(side_items) - 1 for h in hits)}


def report_pack(pack_dir: Path, dump_dir: Path | None = None) -> dict[str, Any]:
    """Every metric for one pack, as a plain dict (also what ``--json`` writes)."""
    collector = _WarningCollector()
    logging.getLogger("app").addHandler(collector)
    try:
        package = _load_pack_artifacts(pack_dir)
        roles = design_pack.detect_roles(pack_dir)
        items = build_plan(pack_dir)
        contract = build_naming_contract_from_package(package)

        agent = CodeGeneratorAgent()
        produced = [f["path"] for f in render_scaffold(pack_dir.name, package)]
        contexts: dict[str, dict[str, Any]] = {}
        for item in items:
            text, sections = agent._assemble_context(item, package, list(produced))
            contexts[item.id] = {"chars": len(text), "sections": sections, "text": text}
            produced.extend(p for p in item.target_files if p not in produced)
    finally:
        logging.getLogger("app").removeHandler(collector)

    role_files = {
        role: sorted(Path(f.path).name if hasattr(f, "path") else str(f) for f in files)
        for role, files in roles.items()
    }

    # --- naming contract: what the "use these EXACT identifiers" block actually contains
    contract_entities = len(re.findall(r"^- `[^`]+`", contract, re.MULTILINE))
    endpoint_block = contract.split("Endpoints --", 1)[1] if "Endpoints --" in contract else ""
    contract_endpoints = len(re.findall(r"^- [A-Z]+ /", endpoint_block, re.MULTILINE))

    # --- per-side context coverage
    side_stats: dict[str, dict[str, Any]] = {}
    for side in ("backend", "frontend"):
        ids = [w.id for w in items if _side(w.id) == side]
        if not ids:
            continue
        sizes = [contexts[i]["chars"] for i in ids]
        section_counts: dict[str, int] = {}
        for i in ids:
            for name in contexts[i]["sections"]:
                section_counts[name] = section_counts.get(name, 0) + 1
        api_with_detail = sum(
            1 for i in ids
            if any(m in _section(contexts[i]["text"], "API") for m in _API_DETAIL_MARKERS)
        )
        side_stats[side] = {
            "items": len(ids),
            "context_chars": {"min": min(sizes), "median": int(statistics.median(sizes)), "max": max(sizes)},
            "items_with_section": dict(sorted(section_counts.items())),
            "items_with_api_detail": api_with_detail,
        }

    # --- validation rules: how many rule groups reach ANY prompt
    rules = package.get("validation-rules.json")
    rule_groups = sorted(rules) if isinstance(rules, dict) else []
    validation_text = "\n".join(_section(c["text"], "Validation rules") for c in contexts.values())
    matched_groups = [g for g in rule_groups if f'"{g}"' in validation_text]

    # --- requirement traceability: which ids land on a work item, and whose TEXT reaches a prompt
    req_role = roles.get("requirements") or []
    requirements = package.get(Path(req_role[0].path).name) if req_role else None
    req_ids = _requirement_ids(requirements) if requirements is not None else {}
    on_items = {rid for w in items for rid in w.requirement_ids}
    all_context = "\n".join(c["text"] for c in contexts.values())
    text_in_prompt = {
        rid for rid, text in req_ids.items()
        if text and text[:60] in all_context
    }

    by_prefix: dict[str, dict[str, Any]] = {}
    for rid in sorted(req_ids):
        prefix = rid.split("-", 1)[0]
        bucket = by_prefix.setdefault(prefix, {"total": 0, "on_items": 0, "text_in_prompt": 0, "unmapped": []})
        bucket["total"] += 1
        if rid in on_items:
            bucket["on_items"] += 1
        else:
            bucket["unmapped"].append(rid)
        if rid in text_in_prompt:
            bucket["text_in_prompt"] += 1

    # --- what the shape-based index can SEE (independent of whether a prompt uses it yet)
    index = PackIndex(package)
    spec = index.openapi() or {}
    operations = sum(
        1 for item in (spec.get("paths") or {}).values() if isinstance(item, dict)
        for method in item if method.lower() in {"get", "post", "put", "patch", "delete"}
    )
    tables = index.tables()
    pack_index = {
        "roles": index.roles(),
        "openapi_operations": operations,
        "openapi_component_schemas": len(((spec.get("components") or {}).get("schemas") or {})),
        "tables": len(tables),
        "tables_with_enums": sum(1 for t in tables if t["enums"]),
        "conventions_chars": len(index.conventions_text()),
        "validation_groups": len(index.validation_rules() or {}),
        "sample_payloads": len(index.sample_payloads() or {}),
        "env_vars": len(index.env_vars()),
        "requirements_with_text": sum(len(v) for k, v in index.requirements().items() if k != "constraints"),
    }

    result: dict[str, Any] = {
        "pack": str(pack_dir),
        "files_loaded": len(package),
        "pack_index": pack_index,
        "roles": role_files,
        "unknown_files": len(role_files.get("unknown", [])),
        "work_items": [
            {
                "id": w.id,
                "targets": len(w.target_files),
                "endpoints": len(w.endpoints),
                "tables": w.tables,
                "screens": len(w.screens),
                "requirement_ids": w.requirement_ids,
                "context_chars": contexts[w.id]["chars"],
                "sections": contexts[w.id]["sections"],
            }
            for w in items
        ],
        "naming_contract": {"chars": len(contract), "entities": contract_entities, "endpoints": contract_endpoints},
        "context": side_stats,
        "validation_rules": {"groups": len(rule_groups), "reaching_a_prompt": len(matched_groups)},
        "requirements": by_prefix,
        "entry_points": {
            "backend": _entry_positions(items, "backend", _BACKEND_ENTRY_NAMES),
            "frontend": _entry_positions(items, "frontend", _FRONTEND_ENTRY_NAMES),
        },
        "warnings": sorted(set(collector.messages)),
    }

    if dump_dir is not None:
        out = dump_dir / pack_dir.name
        out.mkdir(parents=True, exist_ok=True)
        for item_id, ctx in contexts.items():
            (out / f"{item_id}.txt").write_text(ctx["text"], encoding="utf-8")
    return result


def _print(result: dict[str, Any]) -> None:
    print(f"\n=== {result['pack']}")
    print(f"files loaded: {result['files_loaded']}   unknown-role files: {result['unknown_files']}")
    for role, files in sorted(result["roles"].items()):
        if role != "unknown":
            print(f"  role {role:20s} {', '.join(files)}")

    pi = result["pack_index"]
    print(
        f"\npack index (what the pipeline can now FIND): {pi['openapi_operations']} API operations, "
        f"{pi['openapi_component_schemas']} component schemas, {pi['tables']} tables "
        f"({pi['tables_with_enums']} with enums), conventions {pi['conventions_chars']} chars, "
        f"{pi['validation_groups']} validation groups, {pi['sample_payloads']} sample payloads, "
        f"{pi['env_vars']} env vars, {pi['requirements_with_text']} requirements with text"
    )
    for role, names in pi["roles"].items():
        print(f"  index role {role:18s} {', '.join(names)}")

    nc = result["naming_contract"]
    print(f"\nnaming contract: {nc['chars']} chars, {nc['entities']} entities, {nc['endpoints']} endpoints")

    print(f"\nwork items: {len(result['work_items'])}")
    for w in result["work_items"]:
        reqs = ",".join(w["requirement_ids"]) or "-"
        print(
            f"  {w['id']:42s} files={w['targets']:2d} eps={w['endpoints']:2d} tables={len(w['tables']):2d} "
            f"screens={w['screens']:2d} ctx={w['context_chars']:6d}  reqs={reqs}"
        )
        print(f"  {'':42s} sections: {', '.join(w['sections']) or '-'}")

    for side, stats in result["context"].items():
        cc = stats["context_chars"]
        print(f"\n{side}: {stats['items']} items, context chars min/median/max = {cc['min']}/{cc['median']}/{cc['max']}")
        for name, count in stats["items_with_section"].items():
            print(f"  {name:22s} {count}/{stats['items']} items")
        print(f"  {'API with op detail':22s} {stats['items_with_api_detail']}/{stats['items']} items")

    vr = result["validation_rules"]
    print(f"\nvalidation-rule groups reaching a prompt: {vr['reaching_a_prompt']}/{vr['groups']}")

    print("\nrequirements (on a work item / text in some prompt / total):")
    for prefix, b in result["requirements"].items():
        unmapped = f"   unmapped: {', '.join(b['unmapped'])}" if b["unmapped"] else ""
        print(f"  {prefix:5s} {b['on_items']}/{b['text_in_prompt']}/{b['total']}{unmapped}")

    for side, info in result["entry_points"].items():
        where = "; ".join(f"{e['file']} @ {e['position'] + 1}/{e['of']}" for e in info["entries"]) or "none found"
        print(f"\n{side} entry points: {where}   (generated last: {info['all_last']})")

    if result["warnings"]:
        print("\npipeline warnings:")
        for msg in result["warnings"]:
            print(f"  - {msg}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("packs", nargs="+", type=Path, help="design-pack directories")
    parser.add_argument("--json", type=Path, default=None, help="also write all metrics to this JSON file")
    parser.add_argument("--dump-contexts", type=Path, default=None,
                        help="write each work item's assembled context to <dir>/<pack>/<item>.txt")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    logging.basicConfig(level=logging.ERROR)  # warnings are collected into the report instead

    results = []
    for pack in args.packs:
        if not pack.is_dir():
            print(f"skipping {pack}: not a directory", file=sys.stderr)
            continue
        result = report_pack(pack.resolve(), args.dump_contexts)
        _print(result)
        results.append(result)

    if args.json is not None:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
