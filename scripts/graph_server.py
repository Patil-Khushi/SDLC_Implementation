r"""HTTP + SSE bridge that drives the SAME LangGraph pipeline as ``scripts/run_fixture.py`` for the
React frontend (``SDLC/frontend``).

This is NEW integration glue — it does NOT modify the graph, the agents, or any existing service.
It reproduces exactly what ``run_fixture.py`` does (``build_plan`` → pick an executor →
``new_state`` → drive the compiled ``workflow``) but drives the graph with ``workflow.stream(...)``
instead of ``.invoke(...)`` so it can emit one Server-Sent Event per node as the run progresses,
and it exposes the design-pack list + plan the same way ``demo_server.py`` already does.

Deliberately separate from ``demo_server.py``: that server's ``/api/run`` runs a DIFFERENT
(feature_commit-based) code-gen path. This one runs the LangGraph graph — the pipeline validated
on the CLI (see the project memory "frontend-integration-pipeline-choice").

Run (from SDLC_Implementation/, with .venv active):

    python scripts/graph_server.py --port 8200          # default
    .venv\Scripts\python scripts\graph_server.py

Endpoints (CORS-open for the Vite dev server on :5173):
    GET  /api/health                         -> {ok, modes}
    GET  /api/packs                          -> {packs: [name, ...]}
    POST /api/upload        {name, files}    -> {pack, name, files, is_design_pack}
    POST /api/plan          {pack, only?}    -> {count, items: [...]}
    POST /api/plan/revise   {pack, feedback, action, mode, plan}   -> {count, items, revised, note}
    GET  /api/run/stream?pack=&project=&mode=&publish=&only=   -> text/event-stream

The stream emits JSON events: run_start, plan, stage, file, log, report, repo, done, error.
Each ``stage`` event names a real graph node mapped to the frontend's stage id; the frontend
advances its 23-stage visual from these.

Output endpoints — the REAL artifacts the last (or in-flight) run produced, so the frontend's
Output section never renders placeholder data:
    GET  /api/run/files                      -> {available, project, files: [{path, language, ...}]}
    GET  /api/run/file?path=                 -> {path, language, content, truncated}
    GET  /api/run/diff?path=                 -> {available, diff: {path, hunks, ...}}
    GET  /api/run/reports                    -> {available, reports: [{kind, title, content, ...}]}
    GET  /api/run/security                   -> {available, verdict, counts, findings: [...]}
    GET  /api/run/github                     -> {available, repository, branches, commits, ...}
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import shutil
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

_IMPL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_IMPL_DIR))  # so `app.*` imports resolve

from app.config.settings import get_settings  # noqa: E402
from app.graph.graph import workflow  # noqa: E402
from app.graph.state import new_state  # noqa: E402
from app.integrations.executor import Executor, FakeExecutor, set_executor  # noqa: E402
from app.agents.project_qa import ProjectQAAgent, build_facts_for  # noqa: E402
from app.integrations.review_sandbox import is_allowed_repo_url  # noqa: E402
from app.models import WorkItem  # noqa: E402
from app.services import chat_inbox  # noqa: E402
from app.services import design_pack  # noqa: E402
from app.services import llm_gateway  # noqa: E402
from app.services.plan_builder import build_plan  # noqa: E402
from scripts.feature_commit import _DEFAULT_OUT_DIR  # noqa: E402
from scripts.local_executor import LocalDiskExecutor  # noqa: E402


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "fixtures").is_dir():
            return candidate
    return start.parent


_REPO_ROOT = _find_repo_root(_IMPL_DIR)
_FIXTURES_DIR = _REPO_ROOT / "fixtures"
#: Where uploaded design packs are saved (outside fixtures/, so uploads never pollute the samples).
_UPLOADS_DIR = _REPO_ROOT / "uploads"

# ---------------------------------------------------------------- node -> frontend stage mapping

#: Real graph node key -> the frontend's stage id (src/mocks/stages.ts). ``None`` = the node has no
#: dedicated tile in the 23-stage visual (emit it as a log line only). ``refactoring`` is special:
#: the first pass maps to "refactoring"; once Security has run, later passes are the security loop.
_NODE_TO_STAGE: dict[str, str | None] = {
    "scaffold": "scaffold",
    # Brownfield entry (the peer of "scaffold"). Mapped to None ON PURPOSE: the frontend's
    # 23-stage visual (SDLC_Frontend/src/mocks/stages.ts) has no brownfield tiles yet, and emitting
    # a stage id it does not know would leave the UI with an unrenderable stage rather than no
    # stage. It streams as a log line until the frontend grows a brownfield input path — at which
    # point this becomes "acquire-repo" and a tile is added there in the same change.
    "acquire": None,
    "change_plan": None,
    "code_modifier": None,
    "change_gate": None,
    "change_verify": None,
    "change_commit": None,
    "select": "select-work-item",
    "code_generator": "code-generator",
    "gate": "gate",
    "repair": "repair",
    "feature_publish": "feature-publish",
    "reconcile": "reconcile",
    "commit": "commit",
    "code_review": "code-review",
    "refactoring": "refactoring",  # -> "refactoring-security-loop" after security runs
    "refactoring_publish": "refactoring-publish",
    "debug_check": "debug-check",
    "debugging": "debugging",
    "unit_test_generate": "unit-test-generation",
    "unit_test_run": "unit-test-run",
    "debug_publish": None,  # persisted-to-dev step; shown as a log, not a tile
    # Conversational intake, on the feature_publish -> select edge. Mapped to None ON PURPOSE: it
    # runs on EVERY loop pass and is a no-op on almost all of them (chat off, or nothing queued), so
    # a tile would flash 59 times a run and mean nothing. The turns that DO apply a patch announce
    # themselves through the dedicated `chat` SSE event, which carries the plan diff a tile could
    # not. Give it a tile only if the frontend grows a conversation panel with its own stage id.
    "chat_intake": None,
    "documentation": "documentation",
    "security": "security-review",
    "finalize": "finalize",
    "package": "package",
    "escalate": None,  # failure marker — handled via the run's terminal status
}

#: Friendly agent label per node, used for the log stream.
_NODE_LABEL: dict[str, str] = {
    "scaffold": "Scaffold",
    "acquire": "Acquire Repo",
    "change_plan": "Change Planner",
    "code_modifier": "Code Modifier",
    "change_gate": "Change Gate",
    "change_verify": "Change Verify",
    "change_commit": "Change Commit",
    "select": "Select Work Item",
    "code_generator": "Code Generator",
    "gate": "Gate",
    "repair": "Repair",
    "feature_publish": "Feature Publish",
    "reconcile": "Reconcile",
    "commit": "Commit",
    "code_review": "Code Review",
    "refactoring": "Refactoring",
    "refactoring_publish": "Refactoring Publish",
    "debug_check": "Debug Check",
    "debugging": "Debugging",
    "unit_test_generate": "Unit Test Generation",
    "unit_test_run": "Unit Test Run",
    "debug_publish": "Debug Publish",
    "chat_intake": "Chat Intake",
    "documentation": "Documentation",
    "security": "Security Review",
    "finalize": "Finalize",
    "package": "Package",
    "escalate": "Escalate",
}

#: Nodes that own a deterministic check result, and the state key holding it. A node's log level and
#: tile status MUST come from ITS OWN result: nodes return the whole ``WorkflowState``, so a passing
#: ``gate_result`` left over from the code-gen phase is present in every later delta and would paint
#: every debug/test failure green if it were consulted first.
_NODE_RESULT_KEY: dict[str, str] = {
    "gate": "gate_result",
    "debug_check": "debug_result",
    "unit_test_run": "test_result",
}

# ---------------------------------------------------------------- canned LLM for dry-run mode


def _canned_llm_reply(prompt: str, **_kw: Any) -> str:
    """Dry-run stand-in (identical shape to run_fixture.py): return the requested target files.

    Handles BOTH generation prompt shapes. ``code_generator._generate_files`` sends a work item with
    two or more targets down the PER-FILE path, which asks for one named file at a time and never
    prints the "Target files" block — so matching only that block returned ``placeholder.py`` for
    every call, no reply matched its requested path, and the item failed the gate with "0 of 4
    planned files". Since every item in the sample pack has four targets, that made a dry-run
    escalate at work item 1: the whole point of the mode (exercise the wiring with no API key) was
    silently unavailable.
    """
    one = re.search(r"Generate EXACTLY ONE file now: (.+)", prompt)
    if one:
        path = one.group(1).strip()
        return json.dumps({
            "files": [{"path": path, "content": f"# placeholder for {path}\n"}],
            "notes": "dry-run canned content",
        })

    match = re.search(r"Target files \(produce ONLY these\):\n((?:- .+\n?)*)", prompt)
    lines = match.group(1).splitlines() if match else []
    paths = [ln[2:].strip() for ln in lines if ln.startswith("- ")]
    paths = [p for p in paths if p and p != "(none specified)"]
    files = [{"path": p, "content": f"# placeholder for {p}\n"} for p in paths] or [
        {"path": "placeholder.py", "content": "# placeholder\n"}
    ]
    return json.dumps({"files": files, "notes": "dry-run canned content"})


# ---------------------------------------------------------------- FastAPI app

app = FastAPI(title="IMP-001 graph bridge")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5177",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5177",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

# The executor + LLM gateway are process-global (set_executor / gateway patch), so only ONE run may
# be in flight at a time. This lock rejects a second concurrent /api/run/stream rather than letting
# two runs stomp on each other's executor.
_run_lock = threading.Lock()

#: Snapshot of the most recent (or in-flight) run, so the frontend's Output pages can serve the
#: REAL generated artifacts — the files the executor actually wrote, the agents' report Markdown,
#: the Security findings JSON, and the generated repo's git history — instead of placeholder data.
#: Written only by ``_run_events``; read by the ``/api/run/*`` endpoints. ``_run_lock`` already
#: guarantees a single run at a time, so plain dict mutation needs no extra locking.
#: Keys: project, pack, mode, publish, status, executor, state, reports (kind -> path), started_at.
_last_run: dict[str, Any] = {}

#: Never listed on the Files page — dependencies, VCS internals, and build output are not
#: "generated by the pipeline" in any sense the user cares about (and node_modules would swamp
#: the tree with tens of thousands of entries).
_SKIP_DIRS = frozenset({
    ".git", "node_modules", ".py_packages", "__pycache__", ".pytest_cache",
    ".venv", "venv", "dist", "build", ".next", "coverage", ".ruff_cache",
})

#: Monaco language id per extension (the frontend used to derive this locally from the mock;
#: serving it alongside each real file keeps one source of truth).
_LANG_BY_EXT = {
    "py": "python", "js": "javascript", "jsx": "javascript", "mjs": "javascript",
    "cjs": "javascript", "ts": "typescript", "tsx": "typescript", "json": "json",
    "md": "markdown", "yml": "yaml", "yaml": "yaml", "html": "html", "htm": "html",
    "css": "css", "scss": "scss", "sql": "sql", "sh": "shell", "bash": "shell",
    "toml": "toml", "ini": "ini", "cfg": "ini", "env": "ini", "xml": "xml",
    "txt": "plaintext", "csv": "plaintext",
}

#: Cap on the content one /api/run/file call returns — a runaway generated file shouldn't be able
#: to wedge the browser. The response flags ``truncated`` so the UI can say so.
_MAX_FILE_CHARS = 400_000


def _language_for(path: str) -> str:
    """Monaco language id for a generated file path."""
    name = path.rsplit("/", 1)[-1]
    lower = name.lower()
    if lower.startswith("dockerfile"):
        return "dockerfile"
    if lower.startswith(".env"):
        return "ini"
    if "." not in name.lstrip("."):
        return "plaintext"
    return _LANG_BY_EXT.get(lower.rsplit(".", 1)[-1], "plaintext")


def _reports_dir() -> Path:
    """Where the Code Review / Refactoring / Security agents save their reports."""
    try:
        return Path(get_settings().reports_dir)
    except Exception:  # noqa: BLE001 — settings are optional here; fall back to the convention
        return _IMPL_DIR / "reports"


def _norm_rel(path: Any) -> str:
    """Normalize a state-recorded path to the posix, workspace-relative form used as a key."""
    text = str(path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _run_file_paths(rec: dict[str, Any]) -> list[str]:
    """Every file the run actually produced, as paths ``executor.read_file`` accepts.

    Real mode -> walk the LocalDiskExecutor's project directory on disk. Dry-run -> the
    FakeExecutor's in-memory file map (nothing is written to disk in that mode). Listing what the
    executor *has* — rather than replaying the state's bookkeeping — means scaffold output and any
    extra file an agent wrote both show up, which is what "the files generated" means to a user.
    """
    executor = rec.get("executor")
    project = str(rec.get("project") or "")
    if isinstance(executor, LocalDiskExecutor):
        root = executor.root
        base = root / project if project and (root / project).is_dir() else root
        if not base.is_dir():
            return []
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for filename in filenames:
                out.append((Path(dirpath) / filename).relative_to(root).as_posix())
        return sorted(out)
    files = getattr(executor, "files", None)
    if isinstance(files, dict):
        return sorted(str(k) for k in files)
    return []


def _file_kinds(rec: dict[str, Any]) -> dict[str, str]:
    """path -> "scaffold" | "source" | "test", from the run state's own bookkeeping.

    ``scaffold_files`` are recorded project-relative (they feed ``git add`` inside the project),
    while ``generated_code``/``unit_tests`` already carry the project prefix — hence the two
    normalizations. Tests are applied last so a generated test file wins over "source".
    """
    state = rec.get("state") or {}
    project = str(rec.get("project") or "")

    def with_project(path: Any) -> str:
        rel = _norm_rel(path)
        if project and not rel.startswith(f"{project}/"):
            return f"{project}/{rel}"
        return rel

    kinds: dict[str, str] = {}
    for path in state.get("scaffold_files") or []:
        kinds[with_project(path)] = "scaffold"
    for path in state.get("generated_code") or []:
        kinds.setdefault(_norm_rel(path), "source")
    for path in state.get("unit_tests") or []:
        kinds[_norm_rel(path)] = "test"
    return kinds


def _refactored_paths(rec: dict[str, Any]) -> list[str]:
    """Workspace-relative paths the Refactoring agent actually edited this run (``write_file``
    calls recorded in ``WorkflowState["refactored_files"]`` — see ``app/agents/refactoring.py``).
    Normalized the same way ``_run_file_paths`` reports paths, so the two lists can be compared
    directly (e.g. to badge a file "edited by Refactoring" alongside its scaffold/source/test kind).

    Prefers the live value captured the moment the ``refactoring`` node fires (``rec["state"]``
    itself isn't backfilled until the WHOLE run finishes — see the streaming loop) so this reflects
    reality while later stages (Debugging, Security, ...) are still running, not just after "done".
    """
    if "refactored_files" in rec:
        return sorted({_norm_rel(p) for p in (rec.get("refactored_files") or [])})
    state = rec.get("state") or {}
    return sorted({_norm_rel(p) for p in (state.get("refactored_files") or [])})


def _project_relative(rec: dict[str, Any], path: str) -> str:
    """Strip the project-dir prefix — git commands run with ``cwd=project``."""
    project = str(rec.get("project") or "")
    rel = _norm_rel(path)
    return rel[len(project) + 1:] if project and rel.startswith(f"{project}/") else rel


def _iso_mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return ""


def _resolve_pack(pack: str) -> Path:
    """Resolve a pack name (fixtures/ or uploads/) or an absolute/relative path to a directory."""
    p = Path(pack)
    candidates = [p, _FIXTURES_DIR / pack, _UPLOADS_DIR / pack, _REPO_ROOT / pack]
    for c in candidates:
        if c.is_dir():
            return c.resolve()
    raise HTTPException(404, f"design pack not found: {pack!r}")


def _plan_item_dict(w: Any) -> dict[str, Any]:
    return {
        "id": w.id,
        "feature_id": getattr(w, "feature_id", ""),
        "feature_title": getattr(w, "feature_title", ""),
        "requirement_ids": list(getattr(w, "requirement_ids", []) or []),
        "endpoints": list(getattr(w, "endpoints", []) or []),
        "tables": list(getattr(w, "tables", []) or []),
        "screens": list(getattr(w, "screens", []) or []),
        "target_files": list(getattr(w, "target_files", []) or []),
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Liveness, plus the server-side switches the UI must not guess at.

    ``chat_enabled`` matters because ``POST /api/chat`` accepts a message whether or not the
    pipeline will ever drain it — acceptance is deliberately independent of the run. With
    ``CHAT_ENABLED=false`` (the default) ``chat_intake`` is a pass-through, so a queued request
    would sit there forever and the UI would have no way to explain why. Reporting the flag lets it
    say so instead of looking broken.
    """
    settings = get_settings()
    return {
        "ok": True,
        "modes": ["dry-run", "real"],
        "chat_enabled": bool(settings.chat_enabled),
        "chat_turn_cap": int(settings.chat_turn_cap),
    }


@app.get("/api/packs")
def packs() -> dict[str, list[str]]:
    """Design-pack directories under fixtures/ that plan_builder can decompose."""
    found: list[str] = []
    if _FIXTURES_DIR.is_dir():
        for d in sorted(_FIXTURES_DIR.iterdir()):
            if d.is_dir() and design_pack.is_design_pack(d):
                found.append(d.name)
    return {"packs": found}


def _safe_name(name: str) -> str:
    """Folder-name-safe slug for an uploaded pack (no path separators / traversal)."""
    base = (name or "").strip().replace("\\", "/").split("/")[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", base).strip("-.")
    return cleaned or "uploaded-pack"


class UploadFile(BaseModel):
    path: str  # pack-relative path, e.g. "openapi.yaml" or "docs/schema.sql"
    content: str


class UploadRequest(BaseModel):
    name: str
    files: list[UploadFile]


@app.post("/api/upload")
def upload(req: UploadRequest) -> dict[str, Any]:
    """Save an uploaded design pack (a folder or a set of files) under uploads/<name>/ and return
    its resolved path so /api/plan and /api/run/stream can build/run it like any other pack.

    The frontend reads each file as text and posts {path, content}; binary artifacts are dropped by
    the client (the pipeline only consumes text design artifacts, matching _load_pack)."""
    name = _safe_name(req.name)
    if not req.files:
        raise HTTPException(400, "no files uploaded")

    dest_root = (_UPLOADS_DIR / name).resolve()
    # Never let a crafted path escape uploads/<name>/ (path-traversal guard).
    if _UPLOADS_DIR.resolve() not in dest_root.parents and dest_root != (_UPLOADS_DIR / name).resolve():
        raise HTTPException(400, "invalid pack name")

    if dest_root.exists():
        shutil.rmtree(dest_root, ignore_errors=True)
    dest_root.mkdir(parents=True, exist_ok=True)

    written = 0
    for f in req.files:
        rel = Path(f.path.replace("\\", "/"))
        target = (dest_root / rel).resolve()
        if dest_root not in target.parents and target != dest_root:
            continue  # skip anything that would escape the pack dir
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.content, encoding="utf-8")
        written += 1

    if written == 0:
        raise HTTPException(400, "no valid files written")

    return {
        "pack": str(dest_root),
        "name": name,
        "files": written,
        "is_design_pack": design_pack.is_design_pack(dest_root),
    }


class PlanRequest(BaseModel):
    pack: str
    only: str = ""


@app.post("/api/plan")
def plan(req: PlanRequest) -> dict[str, Any]:
    """The work-item decomposition the graph will build — same build_plan() run_fixture.py uses."""
    pack_dir = _resolve_pack(req.pack)
    items = build_plan(pack_dir)
    if req.only:
        needle = req.only.lower()
        items = [w for w in items if needle in w.id.lower()]
    return {"count": len(items), "items": [_plan_item_dict(w) for w in items]}


# ---------------------------------------------------------------- plan review + LLM revision

#: The plan-item fields the frontend exchanges (mirrors ``_plan_item_dict`` + ``PlanItem`` in TS).
_PLAN_FIELDS = ("id", "feature_id", "feature_title", "requirement_ids",
                "endpoints", "tables", "screens", "target_files")
_PLAN_LIST_FIELDS = ("requirement_ids", "endpoints", "tables", "screens", "target_files")


def _coerce_plan_item(raw: Any) -> dict[str, Any] | None:
    """Validate one plan item (from the client or the LLM) into the canonical dict shape.

    Runs it through :class:`WorkItem` (which requires a non-empty ``id`` and rejects unknown
    keys), so a malformed item from the model is dropped rather than shipped. Returns ``None``
    when the item can't be salvaged."""
    if not isinstance(raw, dict):
        return None
    data: dict[str, Any] = {}
    for field in _PLAN_FIELDS:
        value = raw.get(field)
        if field in _PLAN_LIST_FIELDS:
            data[field] = [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else []
        else:
            data[field] = str(value).strip() if value is not None else ""
    if not data.get("id"):
        return None
    try:
        return _plan_item_dict(WorkItem(**data))
    except Exception:  # noqa: BLE001 — a bad item shouldn't sink the whole revision
        return None


_REVISE_SYSTEM = (
    "You are the planning step of an automated code-generation pipeline. You are given the current "
    "IMPLEMENTATION PLAN — a JSON array of work items, each the unit of work the code generator "
    "builds in one pass — and a human reviewer's FEEDBACK on it. Revise the plan so it satisfies "
    "the feedback.\n\n"
    "Rules:\n"
    "- Reply with STRICT JSON only: an object {\"work_items\": [ ... ]}. No prose, no markdown fences.\n"
    "- Each work item keeps this exact shape: id (non-empty string), feature_id, feature_title, "
    "requirement_ids (array of strings), endpoints (array), tables (array), screens (array), "
    "target_files (array of workspace-relative paths).\n"
    "- Apply the feedback faithfully: add, remove, split, merge, rename, or re-scope items and their "
    "target files as it asks. Keep every item the feedback does NOT concern, unchanged.\n"
    "- Keep ids stable where an item is unchanged; give any NEW item a short unique id.\n"
    "- Do not invent files unrelated to the feedback; keep the plan buildable."
)


def _llm_revise_plan(
    base_items: list[dict[str, Any]], feedback: str, action: str
) -> tuple[list[dict[str, Any]], bool, str]:
    """Ask the LLM (the code generator's planner) to revise ``base_items`` per ``feedback``.

    Returns ``(items, revised, note)``. On any failure — no credentials, unparseable reply, every
    item invalid — returns the base plan unchanged with ``revised=False`` and an explanatory note,
    so the caller degrades gracefully instead of erroring.
    """
    intent = "Rebuild and adjust the plan" if action == "reject" else "Revise the plan"
    prompt = (
        f"## Current implementation plan\n```json\n{json.dumps(base_items, indent=2)}\n```\n\n"
        f"## Reviewer feedback\n{feedback}\n\n"
        f"## Task\n{intent} to satisfy the feedback. Reply with STRICT JSON "
        '{"work_items": [ ... ]} only.'
    )
    try:
        raw = llm_gateway.llm_gateway.complete(prompt=prompt, system=_REVISE_SYSTEM)
    except Exception as exc:  # noqa: BLE001 — surface as a graceful fallback, never a 500
        logging.warning("plan revision LLM call failed: %s", exc)
        return base_items, False, (
            "Automatic revision is unavailable (the planner model could not be reached — this needs "
            "Real mode with API credentials). Your feedback was recorded; the plan is unchanged."
        )

    parsed = _extract_json_object(raw)
    work_items = parsed.get("work_items") if isinstance(parsed, dict) else None
    if not isinstance(work_items, list):
        return base_items, False, "The planner did not return a valid plan; the plan is unchanged."

    revised = [item for item in (_coerce_plan_item(w) for w in work_items) if item]
    if not revised:
        return base_items, False, "The revised plan had no valid work items; the plan is unchanged."

    delta = len(revised) - len(base_items)
    shape = f"{len(revised)} work items ({'+' if delta >= 0 else ''}{delta})"
    return revised, True, f"Plan revised from your feedback — now {shape}."


def _extract_json_object(text: str) -> Any:
    """Best-effort parse of a JSON object from an LLM reply (tolerates ```json fences / stray prose)."""
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9]*", "", stripped).strip()
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        start, end = stripped.find("{"), stripped.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(stripped[start:end + 1])
            except (ValueError, TypeError):
                return None
    return None


class ReviseRequest(BaseModel):
    pack: str
    project: str = ""
    feedback: str = ""
    action: str = "request_changes"   # "request_changes" (revise current) | "reject" (rebuild)
    mode: str = "real"                # run mode: skip the LLM entirely in "dry-run"
    only: str = ""
    plan: list[dict[str, Any]] = []   # the plan the human is reviewing (for request_changes)


@app.post("/api/plan/revise")
def plan_revise(req: ReviseRequest) -> dict[str, Any]:
    """Regenerate/update the plan from a human reviewer's feedback (Request Changes / Reject).

    ``reject`` rebuilds a fresh decomposition from the pack; ``request_changes`` revises the plan
    the reviewer is looking at. When feedback is present the LLM (the code generator's planning
    step) applies it — except in ``dry-run`` mode, which has no model and returns the deterministic
    base plan with an explanatory note. NEW integration glue: it only *reads* build_plan + the LLM
    gateway; the planner and graph are untouched.
    """
    pack_dir = _resolve_pack(req.pack)

    # Base plan: reject => rebuild from the pack; request_changes => the reviewer's current plan.
    if req.action == "reject" or not req.plan:
        base_items = [_plan_item_dict(w) for w in build_plan(pack_dir)]
        if req.only:
            needle = req.only.lower()
            base_items = [it for it in base_items if needle in it["id"].lower()]
        rebuilt = True
    else:
        base_items = [item for item in (_coerce_plan_item(p) for p in req.plan) if item]
        rebuilt = False

    feedback = req.feedback.strip()
    if not feedback:
        note = ("Plan rebuilt from the design pack." if rebuilt
                else "No feedback provided — the plan is unchanged.")
        return {"count": len(base_items), "items": base_items, "revised": rebuilt, "note": note}

    if req.mode == "dry-run":
        note = ("Plan rebuilt from the design pack. " if rebuilt else "") + (
            "Dry-run has no planner model, so free-text feedback can't be auto-applied — switch to "
            "Real mode to have the code generator revise the plan from your notes."
        )
        return {"count": len(base_items), "items": base_items, "revised": False, "note": note}

    items, revised, note = _llm_revise_plan(base_items, feedback, req.action)
    return {"count": len(items), "items": items, "revised": revised, "note": note}


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


#: Longest log line forwarded to the UI (compile stderr and tracebacks can be enormous).
_MAX_LOG_TEXT = 400
#: Per-node ceiling, so one chatty node can never flood the log panel.
_MAX_LOG_LINES_PER_NODE = 60

#: Sentinel placed on the SSE queue by the driver thread once the run is fully finished (success,
#: error, or early return) — tells the consuming generator to stop waiting for more items.
_DONE = object()

#: How many SSE frames one run keeps for replay. A 59-item run emits a few thousand (log lines
#: dominate, capped per node by _MAX_LOG_LINES_PER_NODE), so this is generous; at ~200 bytes a
#: frame the ceiling is a few MB, which is the right trade for a single-operator local bridge.
_MAX_REPLAY_FRAMES = 20_000
#: Frames never trimmed. run_start and plan live at the very front, and a client that reattaches
#: without them cannot rebuild the board at all — so trimming eats the oldest LOG-era frame after
#: the head rather than the literal oldest frame.
_REPLAY_HEAD_KEEP = 50


class _Broadcast:
    """Fan-out sink for one run's SSE frames, with a replay buffer so a dropped client can reattach.

    The problem this solves: a client whose connection drops cannot simply re-open
    ``/api/run/stream``. That path acquires ``_run_lock`` in order to START a run, and the original
    run's driver thread is still holding it — a network blip in the browser does not stop a
    background thread that is three hours into generating code — so the reconnect was told "a run
    is already in progress" and gave up. The run kept going, invisibly, with no way back to it.

    So readers and runs are now separate concerns. The driver publishes here; any number of readers
    subscribe. ``_run_lock`` still admits exactly one RUN, but no longer limits how many clients
    can watch it, or when they may join.

    One instance module-wide rather than one per project, deliberately: the executor and LLM
    gateway are process-global, so only one run can exist at a time anyway (the same reason
    ``_last_run`` is a single dict).

    Frames are buffered as already-serialized SSE strings — replay is then a verbatim re-yield,
    with no risk of the second rendering differing from the first.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[queue.Queue[Any]] = []
        self._buffer: list[str] = []
        self.project = ""
        self.kind = ""          # "pack" | "brownfield" — how a client should read these frames
        self.active = False     # is a driver still publishing?

    def reset(self, *, project: str, kind: str) -> None:
        """Begin a new run: release the previous run's readers and drop its buffer."""
        with self._lock:
            subs, self._subs = self._subs, []
            self._buffer = []
            self.project = project
            self.kind = kind
            self.active = True
        for q in subs:  # a straggler still reading the PREVIOUS run must not hang forever
            q.put(_DONE)

    def put(self, line: str) -> None:
        """Publish one already-serialized SSE frame. Same signature as ``queue.Queue.put`` so the
        driver threads and ``_SSELogCapture`` can treat this as their sink unchanged."""
        with self._lock:
            self._buffer.append(line)
            if len(self._buffer) > _MAX_REPLAY_FRAMES:
                del self._buffer[_REPLAY_HEAD_KEEP]
            subs = list(self._subs)
        for q in subs:
            q.put(line)

    def subscribe(self) -> "queue.Queue[Any]":
        """A live queue for the run's own driver-side reader (the launching request)."""
        q: "queue.Queue[Any]" = queue.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[Any]") -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def attach(self, project: str) -> tuple[str, str, list[str], "queue.Queue[Any] | None"]:
        """Join an existing run. Returns ``(status, kind, buffered_frames, live_queue)``.

        ``status`` is ``"running"`` (frames replayed, live queue follows), ``"finished"`` (frames
        replayed, nothing more coming) or ``"none"`` (this project is not the run we hold).

        The snapshot and the subscribe happen under ONE lock acquisition. That is the whole
        correctness argument: a frame published between "copy the buffer" and "register the queue"
        would otherwise be either lost or delivered twice, and a client rebuilding a stage board
        from a replay cannot tell the difference.
        """
        with self._lock:
            if not self.project or (project and project != self.project):
                return "none", self.kind, [], None
            frames = list(self._buffer)
            if not self.active:
                return "finished", self.kind, frames, None
            q: "queue.Queue[Any]" = queue.Queue()
            self._subs.append(q)
            return "running", self.kind, frames, q

    def snapshot_meta(self) -> tuple[int, bool]:
        """``(buffered frame count, is a driver still publishing)`` — for the cheap JSON probe."""
        with self._lock:
            return len(self._buffer), self.active

    def close(self) -> None:
        """The driver is done: stop accepting readers and release every current one."""
        with self._lock:
            self.active = False
            subs, self._subs = self._subs, []
        for q in subs:
            q.put(_DONE)


#: The single live-run event bus (see _Broadcast — one run at a time, process-wide).
_bus = _Broadcast()


class _SSELogCapture(logging.Handler):
    """Pushes ``app.*`` log records onto the run's SSE queue AS THEY HAPPEN.

    The agents already log their real work — which test files the Unit Test agent wrote per work
    item, that a Debugging fix failed to parse and nothing was written, why a check blew up — but
    those records only ever reached the server console. This mirrors them into SSE ``log`` events.

    A single-node ``workflow.stream()`` chunk only arrives once that WHOLE node has finished, so a
    node that loops internally over many items (Unit Test Generation runs one LLM call per work
    item inside ONE node invocation) used to have its logs buffered and dumped in one burst at the
    end. Log records are emitted from LangGraph's OWN worker thread while the node's body is still
    running (confirmed empirically: a ``stream_mode="tasks"`` start event for a node arrives before
    that node's body executes a single line), so pushing straight onto the queue here — instead of
    buffering into a list for the driver to drain later — makes every log line reach the frontend
    the moment the agent emits it, node-boundary or not.

    ``current_label`` is a 2-element list shared with the driver thread: ``[0]`` is the friendly
    agent label to tag the NEXT emitted record with (flipped by the driver the instant a
    ``stream_mode="tasks"`` start event names the node about to run — see ``_drive``), and ``[1]``
    is a running count used to enforce ``_MAX_LOG_LINES_PER_NODE`` per node, reset alongside the
    label. A benign, GIL-safe race (a handful of log lines from the tail of one node landing just
    before the label flips to the next) is possible but immaterial — worst case one or two lines
    are mislabeled by a few milliseconds' overlap, never lost or duplicated.
    """

    def __init__(self, sink: "queue.Queue[Any]", current_label: list[Any]) -> None:
        super().__init__(level=logging.INFO)
        self._sink = sink
        self._current_label = current_label

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage().strip()
        except Exception:  # noqa: BLE001 - a bad format string must never break the run
            return
        if not text or text.startswith("="):  # nodes.py's _stage() banner — the stage tile says this
            return
        if text.startswith("->"):  # ...and its "   -> <doing>" second line reads better unprefixed
            text = text[2:].strip()
        if len(text) > _MAX_LOG_TEXT:
            text = text[: _MAX_LOG_TEXT - 1] + "..."
        level = "warn" if record.levelno >= logging.WARNING else "info"

        label = self._current_label[0]
        count = self._current_label[1]
        if getattr(record, "ui_always", False):
            # A record an agent marked as its stage's outcome (e.g. a debugging round's closing
            # summary) is forwarded even past the cap: a long tool loop alone exceeds it, and the
            # cap used to drop exactly the line saying what the stage achieved.
            self._sink.put(_sse({"type": "log", "agent": label, "level": level, "text": text}))
            return
        if count > _MAX_LOG_LINES_PER_NODE:
            return  # already told the frontend lines are being omitted for this node
        if count == _MAX_LOG_LINES_PER_NODE:
            self._current_label[1] = count + 1
            self._sink.put(_sse({"type": "log", "agent": label, "level": "info",
                                  "text": "... further log lines omitted for this stage"}))
            return
        self._current_label[1] = count + 1
        self._sink.put(_sse({"type": "log", "agent": label, "level": level, "text": text}))


@contextmanager
def dry_run_gateway() -> Iterator[None]:
    """Patch the process-global LLM gateway to canned replies, and ALWAYS put it back.

    ``llm_gateway.llm_gateway`` is a singleton that every agent binds at construction
    (``app/agents/base.py``), so patching it is process-wide and outlives the run that did it. It
    used to be patched here and never restored: after one dry-run, every subsequent *real* run in
    the same server process silently generated canned placeholder files, and ``/api/plan/revise``
    returned canned JSON that parsed as "the planner did not return a valid plan". Restarting the
    server was the only reset.

    A context manager rather than an inline try/finally so the restore is provable in a test — and
    so it cannot be separated from the patch by a later edit.

    Restores by DELETING the instance attribute when there was not one to begin with, rather than
    writing back the bound method it read. Reading ``instance.method`` on a class-defined method
    creates a fresh bound object each time, so assigning one back would leave a permanent instance
    attribute holding a redundant self-reference — pristine-looking but not pristine. Deleting
    returns the object to normal class lookup, which is what "unpatched" actually means.
    """
    gateway = llm_gateway.llm_gateway
    sentinel = object()
    original = {
        name: gateway.__dict__.get(name, sentinel)
        for name in ("complete", "complete_with_tools")
    }

    gateway.complete = _canned_llm_reply  # type: ignore[method-assign]
    gateway.complete_with_tools = (  # type: ignore[method-assign]
        lambda prompt, **kw: _canned_llm_reply(prompt)
    )
    try:
        yield
    finally:
        for name, prior in original.items():
            if prior is sentinel:
                gateway.__dict__.pop(name, None)
            else:
                gateway.__dict__[name] = prior


class ChatRequest(BaseModel):
    project: str
    text: str


@app.post("/api/chat", status_code=202)
def chat(req: ChatRequest) -> dict[str, Any]:
    """Queue a change request for a project, and return immediately.

    **202, and deliberately nothing else.** This handler must be callable at any instant — including
    while work item 23 of 59 is blocked inside a multi-minute LLM call — so it touches none of the
    run's machinery: it does not acquire ``_run_lock``, does not read or write ``_last_run``, and
    never waits on the driver thread. All it does is append one fsync'd line to the inbox.

    Application happens later and elsewhere: ``chat_intake_node`` drains the inbox at the next work
    item boundary. Separating acceptance from application is what makes a conversational build work
    without an ``interrupt()`` and without the run needing to be idle — and it is why a message
    survives even if the run it was aimed at then crashes.
    """
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text must not be empty")
    project = (req.project or "").strip()
    if not project:
        raise HTTPException(status_code=400, detail="project must not be empty")

    try:
        seq = chat_inbox.append(
            project,
            text,
            received_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not queue the message: {exc}") from exc

    pending = chat_inbox.pending_count(project, _consumed_seq(project))
    return {"seq": seq, "queued": pending, "project": project}


@app.post("/api/chat/message")
def chat_message(req: ChatRequest) -> dict[str, Any]:
    """One channel, two intents: answer a question NOW, or queue a change request.

    The chat could previously only mutate the plan, so a question ("how many work items are left?",
    "what did the security scan find?") reached the change planner and came back as its refusal —
    correct for a change, useless as a reply to a question.

    So this classifies first (``ProjectQAAgent``, one LLM call) and then:

    * **question** — answered synchronously, right here, from the run's facts plus read-only access
      to the generated files. A read needs no plan mutation, no lock and no work-item boundary;
      routing it through the graph would mean waiting minutes to be told what is already known.
    * **change** — appended to the inbox exactly as ``POST /api/chat`` does, for
      ``chat_intake_node`` to apply at the next boundary with a validated file list.

    Notably it does NOT take ``_run_lock``: answering is read-only, and blocking a question behind a
    3-hour build would defeat the point. It does make an LLM call from a request handler, which
    ``/api/plan/revise`` already does. Two consequences worth knowing: in dry-run the process-global
    gateway is patched to canned replies, so answers there are canned too; and the answer describes
    the run's state at the moment it was asked, which on a live run is a moving target.
    """
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text must not be empty")
    project = (req.project or "").strip()
    if not project:
        raise HTTPException(status_code=400, detail="project must not be empty")

    # Answer against the run we actually hold. A question about a DIFFERENT project than the one
    # loaded would otherwise be answered from the wrong facts, confidently.
    same_project = _last_run.get("project") == project
    facts = build_facts_for(_last_run) if same_project else ""
    executor = _last_run.get("executor") if same_project else None

    agent = ProjectQAAgent(executor=executor if isinstance(executor, Executor) else None)
    result = agent.ask(text, facts=facts, project_dir=project if same_project else "")

    if result.error:
        # Fall back to treating it as a change request rather than losing the message: queuing is
        # the behaviour that existed before this endpoint, and it is recoverable.
        seq = _queue_change(project, text)
        return {
            "kind": "change", "seq": seq, "project": project,
            "queued": chat_inbox.pending_count(project, _consumed_seq(project)),
            "note": f"Could not classify this message ({result.error}), so it was queued as a "
                    "change request.",
        }

    if result.kind == "change":
        seq = _queue_change(project, text)
        return {
            "kind": "change", "seq": seq, "project": project,
            "queued": chat_inbox.pending_count(project, _consumed_seq(project)),
            # A change request can still carry a question worth answering immediately.
            "answer": result.answer,
        }

    return {
        "kind": "answer", "project": project, "answer": result.answer,
        "grounded": bool(facts),
        "note": "" if same_project else
                f"No run named {project!r} is loaded, so this was answered without run facts.",
    }


def _queue_change(project: str, text: str) -> int:
    """Append one change request to the inbox; shared by both chat endpoints."""
    try:
        return chat_inbox.append(
            project,
            text,
            received_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not queue the message: {exc}") from exc


def _consumed_seq(project: str) -> int:
    """How far the live run has drained this project's inbox (0 when no run of it is in flight)."""
    if _last_run.get("project") != project:
        return 0
    return int((_last_run.get("state") or {}).get("chat_seq", 0) or 0)


@app.get("/api/chat/log")
def chat_log(project: str) -> dict[str, Any]:
    """Everything accepted for ``project``, plus what the live run has actually applied.

    Follows the artifact endpoints' convention: 200 with ``available: false`` and a reason rather
    than a 4xx, so the UI renders an explanation instead of an error.
    """
    project = (project or "").strip()
    if not project:
        return {"available": False, "reason": "no project given", "messages": [], "turns": []}

    messages = chat_inbox.read_new(project, 0)
    applied = (_last_run.get("state") or {}).get("chat_log") or [] \
        if _last_run.get("project") == project else []
    if not messages and not applied:
        return {
            "available": False,
            "reason": f"nothing has been sent for {project!r} yet",
            "messages": [], "turns": [],
        }
    return {
        "available": True,
        "project": project,
        "messages": messages,
        "consumed_seq": _consumed_seq(project),
        "turns": applied,
    }


# ---------------------------------------------------------------- Brownfield (existing repo + a
# change request), the frontend-facing bridge for scripts/run_change_request.py's lane.
#
# Deliberately a SEPARATE endpoint pair and a separate SSE event vocabulary from the pack-driven
# run above, not a mode flag on it: the two lanes take different inputs (repo_url + change_request
# vs. a design pack), use a different executor rooting (workspace/<project>-<run>, never
# generated-apps/ — that directory is greenfield output and _run_events wipes it on every real run),
# and the brownfield graph shape has no stage tiles (_NODE_TO_STAGE maps every brownfield node to
# None on purpose — see that map's comment). Reusing _run_events' pack-shaped setup would mean
# threading a second, unrelated input path through code that already carries a lot of state.

_BROWNFIELD_KINDS = ("bug", "feature", "modification")

#: repo_url/change_request submitted via POST, waiting for the matching GET stream to claim them.
#: Token-addressed rather than passed as query params on the stream URL itself: EventSource is a
#: GET with no body, and a multi-line description plus lists of acceptance criteria/constraints
#: does not belong in a query string. In-memory only — this is a single-operator local bridge, the
#: same trust model as the rest of this server; an entry is consumed (popped) by the one stream
#: that claims it, so it cannot be replayed.
_pending_change_requests: dict[str, dict[str, Any]] = {}
_pending_lock = threading.Lock()


class ChangeRequestSubmission(BaseModel):
    repo_url: str
    title: str = ""
    kind: str = "modification"
    description: str
    acceptance_criteria: list[str] = []
    constraints: list[str] = []
    base_ref: str = ""


@app.post("/api/brownfield/change-request", status_code=201)
def submit_change_request(req: ChangeRequestSubmission) -> dict[str, Any]:
    """Validate and queue a change request; return a token the stream endpoint claims.

    Validation happens here, at submission, rather than only inside ``acquire_repo_node`` — the
    node's checks are the ones that matter (defense in depth, not duplicated for its own sake), but
    failing a malformed request before a background thread and a queue even exist gives a plain
    HTTP error instead of one more SSE `error` event to parse.
    """
    repo_url = req.repo_url.strip()
    if not is_allowed_repo_url(repo_url):
        raise HTTPException(
            status_code=400,
            detail=f"{repo_url!r} is not an allowed repository URL "
                    "(expected a public https://github.com/<owner>/<repo>)",
        )
    description = req.description.strip()
    if not description:
        raise HTTPException(status_code=400, detail="description must not be empty")
    kind = req.kind.strip() or "modification"
    if kind not in _BROWNFIELD_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {_BROWNFIELD_KINDS}, got {kind!r}")

    token = uuid.uuid4().hex
    change_request = {
        "id": token[:8],
        "title": req.title.strip() or description[:80],
        "kind": kind,
        "description": description,
        "acceptance_criteria": [c.strip() for c in req.acceptance_criteria if c.strip()],
        "constraints": [c.strip() for c in req.constraints if c.strip()],
    }
    with _pending_lock:
        _pending_change_requests[token] = {
            "repo_url": repo_url,
            "base_ref": req.base_ref.strip(),
            "change_request": change_request,
        }
    return {"token": token}


#: Curated per-node summaries for the brownfield stream — see ``_run_events_brownfield``. Kept in
#: one place so adding a field to a node's output is a one-line change, not a scan of the loop body.
def _brownfield_detail(node: str, delta: dict[str, Any]) -> dict[str, Any]:
    if node == "acquire":
        inv = delta.get("repo_inventory") or {}
        return {
            "baseBranch": delta.get("base_branch") or "",
            "baseSha": (delta.get("base_sha") or "")[:12],
            "branch": delta.get("branch") or "",
            "files": len(inv.get("files") or []) if inv else None,
            "sourceFiles": len(inv.get("source_files") or []) if inv else None,
            "primaryLanguage": inv.get("primary_language") or "",
        }
    if node == "change_plan":
        items = delta.get("work_items") or []
        errors = delta.get("change_plan_errors") or []
        impacts = delta.get("change_impacts") or {}
        return {
            "itemCount": len(items),
            "itemIds": [w.id if hasattr(w, "id") else w.get("id") for w in items][:20],
            "errors": errors,
            "impactedFileCount": len({d for deps in impacts.values() for d in deps}),
        }
    if node == "change_gate":
        result = delta.get("gate_result") or {}
        checks = result.get("checks") or []
        return {
            "passed": bool(result.get("passed")),
            "checks": [
                {"name": c.get("name"), "passed": c.get("passed"),
                 "stderr": (c.get("stderr") or "")[:400]}
                for c in checks
            ],
        }
    if node == "change_verify":
        return {
            "verdict": delta.get("verify_verdict") or "",
            "testCommand": delta.get("test_command") or "",
        }
    if node == "change_commit":
        return {"changedFiles": delta.get("changed_files") or []}
    if node == "finalize":
        return {
            "prUrl": delta.get("pr_url") or "",
            "status": delta.get("finalize_status") or "",
        }
    if node == "package":
        return {"packagePath": delta.get("package_path") or ""}
    return {}


def _run_events_brownfield(*, token: str, project: str, push: bool) -> Iterator[str]:
    """Drive a brownfield (existing repo + change request) run; yield SSE lines as it progresses.

    Structurally mirrors ``_run_events``'s lock/queue/capture-thread pattern (same reasons apply: a
    disconnected client must not abort a run still editing a clone, and the driver thread is the
    only place holding ``_run_lock``/the executor) but is otherwise independent — see the module
    comment above this section for why the two are not merged.
    """
    if not _run_lock.acquire(blocking=False):
        yield _sse({"type": "error", "message": "A run is already in progress. Try again shortly."})
        return

    with _pending_lock:
        pending = _pending_change_requests.pop(token, None)
    if pending is None:
        _run_lock.release()
        yield _sse({"type": "error", "message": "Unknown or already-used token. Submit the change request again."})
        return

    # Publish through the bus rather than a private queue: it keeps a replay buffer and fans out to
    # any number of readers, which is what lets a dropped client rejoin this run through
    # /api/run/attach instead of losing it. `sse_q` remains the driver's sink (the bus has the same
    # put() signature, so nothing downstream changes); `reader` is THIS request's live queue.
    _bus.reset(project=project, kind="brownfield")
    sse_q: Any = _bus
    reader = _bus.subscribe()
    current_label: list[Any] = ["System", 0]
    capture = _SSELogCapture(sse_q, current_label)
    app_logger = logging.getLogger("app")

    def _drive() -> None:
        restore_level: int | None = None
        if not app_logger.isEnabledFor(logging.INFO):
            restore_level = app_logger.level
            app_logger.setLevel(logging.INFO)
        app_logger.addHandler(capture)
        try:
            repo_url = pending["repo_url"]
            run_id = uuid.uuid4().hex[:8]
            workspace = _IMPL_DIR / "workspace" / f"{project}-{run_id}"

            git_remote = ""
            git_token = ""
            if push:
                match = re.match(r"^https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", repo_url)
                if not match:
                    sse_q.put(_sse({"type": "error", "message":
                                    f"push needs a https://github.com/<owner>/<repo> URL, got {repo_url!r}"}))
                    return
                git_remote = f"{match.group(1)}/{match.group(2)}"
                git_token = os.environ.get("GITHUB_PAT", "").strip()

            executor = LocalDiskExecutor(workspace)
            set_executor(executor)

            sse_q.put(_sse({
                "type": "run_start", "run_id": f"{project}-{run_id}", "project": project,
                "repoUrl": repo_url, "push": push,
            }))

            state = new_state(
                run_id=f"{project}-{run_id}", attempt=0, project_id=project,
                source_mode="brownfield", source_repo_url=repo_url,
                change_request=pending["change_request"], base_ref=pending["base_ref"],
                push_enabled=push, git_remote=git_remote, git_token=git_token,
            )
            # A per-run thread_id, never just the project name: LangGraph MERGES an invoke input
            # onto whatever checkpoint already exists for the id, so reusing one across runs would
            # silently inherit a prior run's fields (e.g. a stale gate_result rerouting the graph).
            config = {"configurable": {"thread_id": f"{project}-{run_id}"}, "recursion_limit": 1000}

            for stream_mode_name, chunk in workflow.stream(state, config, stream_mode=["updates", "tasks"]):
                if stream_mode_name == "tasks":
                    if "input" not in chunk:
                        continue
                    node = chunk.get("name", "")
                    current_label[0] = _NODE_LABEL.get(node, node)
                    current_label[1] = 0
                    continue

                for node, delta in chunk.items():
                    if not isinstance(delta, dict):
                        continue
                    label = _NODE_LABEL.get(node, node)
                    failed = delta.get("workflow_status") == "needs_human_review"
                    if node == "change_gate" and "gate_result" in delta:
                        failed = not (delta["gate_result"] or {}).get("passed", True)
                    sse_q.put(_sse({
                        "type": "brownfield_step", "node": node, "label": label,
                        "status": "failed" if failed else "completed",
                        "detail": _brownfield_detail(node, delta),
                    }))

            final = workflow.get_state(config).values
            sse_q.put(_sse({
                "type": "done",
                "status": final.get("workflow_status") or "",
                "branch": final.get("branch") or "",
                "baseBranch": final.get("base_branch") or "",
                "prUrl": final.get("pr_url") or "",
                "changeReportPath": final.get("change_report_path") or "",
                "packagePath": final.get("package_path") or "",
                "changedFiles": final.get("changed_files") or [],
                "workspace": str(workspace),
            }))
        except Exception as exc:  # noqa: BLE001 - report to the client, don't just die silently
            logger.exception("brownfield run failed")
            sse_q.put(_sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"}))
        finally:
            # close() releases every reader, including any that attached mid-run — not just the
            # request that launched it.
            _bus.close()
            app_logger.removeHandler(capture)
            if restore_level is not None:
                app_logger.setLevel(restore_level)
            _run_lock.release()

    driver = threading.Thread(target=_drive, name=f"brownfield-driver-{project}", daemon=True)
    try:
        driver.start()
    except Exception as exc:  # noqa: BLE001 - couldn't start the thread; release synchronously
        _bus.close()
        _run_lock.release()
        yield _sse({"type": "error", "message": f"could not start run: {type(exc).__name__}: {exc}"})
        return

    try:
        while True:
            item = reader.get()
            if item is _DONE:
                break
            yield item
    finally:
        # This request is gone (finished, or the client disconnected). Drop its queue so the driver
        # stops filling a buffer nobody drains — the run itself continues, and a later attach picks
        # it up from the replay buffer.
        _bus.unsubscribe(reader)


@app.get("/api/brownfield/stream")
def brownfield_stream(token: str, project: str, push: bool = False) -> StreamingResponse:
    project = (project or "").strip()
    if not project:
        raise HTTPException(status_code=400, detail="project must not be empty")
    return StreamingResponse(
        _run_events_brownfield(token=token, project=project, push=push),
        media_type="text/event-stream",
    )


def _failed_checks(result: Any) -> list[tuple[str, str]]:
    """``(check name, one-line stderr)`` for every failing check in a gate/debug/test result."""
    out: list[tuple[str, str]] = []
    if not isinstance(result, dict):
        return out
    for check in result.get("checks") or []:
        if not isinstance(check, dict) or check.get("passed", True):
            continue
        stderr = " ".join(str(check.get("stderr", "")).split())
        if len(stderr) > _MAX_LOG_TEXT:
            stderr = stderr[: _MAX_LOG_TEXT - 1] + "..."
        out.append((str(check.get("name", "check")), stderr))
    return out


def _run_events(
    *, pack: str, project: str, mode: str, publish: bool, only: str
) -> Iterator[str]:
    """Drive the compiled workflow and yield SSE lines in real time as the run progresses.

    The graph itself is driven in a BACKGROUND THREAD (``_drive``, below) that pushes SSE-ready
    strings onto ``sse_q``; this generator just drains that queue and yields. That split is what
    makes a node's OWN log lines stream out AS THEY HAPPEN rather than in one lump when the whole
    node finishes: LangGraph runs a node's body on its own worker thread and only hands the
    ``updates`` chunk back to whoever is iterating ``.stream()`` once that body returns, so a node
    that loops internally over many items (Unit Test Generation makes one LLM call per work item
    inside a SINGLE node) used to buffer every one of those calls' log lines until the whole loop
    was done. Adding ``stream_mode="tasks"`` alongside ``"updates"`` gives a task-START event the
    instant a node is dispatched — confirmed empirically to arrive before that node's body runs a
    single line — so the driver can flip ``current_label`` to the right agent name in time for
    ``_SSELogCapture`` (which pushes straight onto the SAME queue from inside the node's own
    thread) to tag that node's log lines correctly, live, from the very first line.

    Mirrors run_fixture.py's setup exactly otherwise; no graph/agent logic is changed.
    """
    if not _run_lock.acquire(blocking=False):
        yield _sse({"type": "error", "message": "A run is already in progress. Try again shortly."})
        return

    # Publish through the bus rather than a private queue: it keeps a replay buffer and fans out to
    # any number of readers, which is what lets a dropped client rejoin this run through
    # /api/run/attach instead of losing it. `sse_q` remains the driver's sink (the bus has the same
    # put() signature, so nothing downstream changes); `reader` is THIS request's live queue.
    _bus.reset(project=project, kind="pack")
    sse_q: Any = _bus
    reader = _bus.subscribe()
    current_label: list[Any] = ["System", 0]  # [0]=agent label, [1]=lines emitted for it so far
    capture = _SSELogCapture(sse_q, current_label)
    app_logger = logging.getLogger("app")

    def _drive() -> None:
        """Runs on a background thread, for the FULL lifetime of the run — independent of whether
        anyone is still iterating the SSE generator below. A disconnected client (closed tab, lost
        connection) must never abort, nor block on, a run that's still generating code; it must
        also never be able to leave a SECOND request thinking a run is still in progress after the
        first one actually finished. Both requirements mean this function — not the generator's own
        teardown — is the ONLY place that installs/removes the "app" logger handler and acquires/
        releases ``_run_lock``: this thread demonstrably keeps running (and keeps mutating
        set_executor()/``_last_run``) after a client disconnects the SSE generator abandons its
        iteration and calls ``GeneratorExit`` on whatever it's currently doing, which Starlette's
        ``StreamingResponse`` can trigger from INSIDE the asyncio event loop's own thread on
        teardown — a blocking join() there would freeze the entire server, not just this request,
        for up to the run's full remaining duration.
        """
        restore_level: int | None = None
        # The active dry-run gateway patch, entered in the dry-run branch and exited in the
        # finally. None = never patched, so nothing to put back.
        gateway_patch: Any = None
        if not app_logger.isEnabledFor(logging.INFO):  # respect, then restore, the server's config
            restore_level = app_logger.level
            app_logger.setLevel(logging.INFO)
        app_logger.addHandler(capture)
        try:
            pack_dir = _resolve_pack(pack)
            design_package = _load_pack(pack_dir)
            work_items = build_plan(pack_dir)
            if only:
                needle = only.lower()
                work_items = [w for w in work_items if needle in w.id.lower()]
            if not work_items:
                sse_q.put(_sse({"type": "error", "message": f"No work items for pack {pack!r} (only={only!r})."}))
                return

            sse_q.put(_sse({
                "type": "run_start", "run_id": project, "project": project,
                "mode": mode, "pack": pack_dir.name, "publish": publish,
            }))
            sse_q.put(_sse({
                "type": "plan", "count": len(work_items),
                "items": [_plan_item_dict(w) for w in work_items],
            }))

            # Executor + push config — same choices as run_fixture.py.
            executor: Executor
            push_enabled = False
            git_remote = ""
            git_token = ""
            if mode == "dry-run":
                # Entered here, exited in this function's finally — see dry_run_gateway's docstring
                # for why the restore is mandatory rather than tidy.
                gateway_patch = dry_run_gateway()
                gateway_patch.__enter__()
                executor = FakeExecutor()
            else:  # real
                out_base = (_DEFAULT_OUT_DIR).resolve()
                # `out_base / project` is a FIXED, persistent directory (never scoped to a single
                # run) — `project_id=project` below makes it the exact path every node.py check
                # reads/writes. Left alone across runs, a second run of the same project name
                # silently reuses yesterday's generated files AND its local .git history: commits
                # stack on top of old ones, and publish()'s `git push origin --all` then pushes
                # that whole retained history to a freshly (re)created GitHub repo — so a repo the
                # user just deleted and expects to be empty shows old files with old commit dates.
                # Wiping it here (real mode only — dry-run uses FakeExecutor, no disk footprint)
                # guarantees every run starts from a genuinely clean project directory and .git.
                # This runs BEFORE LocalDiskExecutor exists, so it can't rely on that executor's
                # own _resolve() root-escape guard — `project` is a raw, unvalidated query param,
                # so re-check the same invariant here before an unguarded shutil.rmtree.
                stale_dir = (out_base / project).resolve()
                if stale_dir != out_base and out_base not in stale_dir.parents:
                    raise ValueError(f"project name escapes the output root: {project!r}")
                if stale_dir.exists():
                    sse_q.put(_sse({
                        "type": "log", "agent": "System", "level": "info",
                        "text": f"Clearing stale output from a previous run: {stale_dir}",
                    }))
                    shutil.rmtree(stale_dir, ignore_errors=True)
                executor = LocalDiskExecutor(out_base, private=not publish)
                if publish:
                    owner = os.environ.get("GITHUB_OWNER", "").strip()
                    if not owner:
                        owner = executor.run_command(
                            ["gh", "api", "user", "--jq", ".login"]
                        ).stdout.strip()
                    git_remote = f"{owner}/{project}"
                    git_token = os.environ.get("GITHUB_PAT", "").strip()
                    push_enabled = True
                    sse_q.put(_sse({"type": "log", "agent": "System", "level": "info",
                                    "text": f"Publishing to github.com/{git_remote} during the run"}))
            set_executor(executor)

            # Hand the Output endpoints everything they need to serve this run's real artifacts.
            _last_run.clear()
            _last_run.update({
                "project": project, "pack": pack_dir.name, "mode": mode, "publish": publish,
                "status": "running", "executor": executor, "state": {}, "reports": {},
                "plan_count": len(work_items),
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "finished_at": "",
            })

            initial = new_state(
                run_id=project, attempt=0, project_id=project,
                design_package=design_package, work_items=work_items,
                push_enabled=push_enabled, git_remote=git_remote, git_token=git_token,
                # Conversational turns are opt-in per deployment (CHAT_ENABLED). Off => chat_intake
                # is a pass-through and this run behaves exactly as it did before the feature.
                chat_enabled=bool(get_settings().chat_enabled),
            )
            # This run owns only messages sent WHILE it runs. Seeding the mark past whatever the
            # inbox already holds stops a project's conversational history being re-applied to a
            # fresh build (the inbox outlives the run - it lives outside the project dir).
            initial["chat_seq"] = chat_inbox.high_water(project)
            # A per-run thread_id, never just the project name (same reason as the brownfield path
            # above): LangGraph MERGES an invoke input onto whatever checkpoint already exists for
            # the id, so a second build of the same project silently inherited the previous run's
            # debugging_report, unit_tests, test_result and security_verdict — a stale
            # security_verdict alone reroutes route_after_refactoring past the debug/test loop.
            config = {
                "configurable": {"thread_id": f"{project}-{uuid.uuid4().hex[:8]}"},
                "recursion_limit": 1000,
            }

            seen_files: set[str] = set()
            seen_reports: set[str] = set()
            repo_sent = False
            security_seen = False
            # Highest conversational turn already announced, so a turn is reported exactly once
            # (see the chat_intake branch below).
            last_chat_seq = 0

            # "tasks" gives a start event (node name) the instant a node is dispatched — BEFORE
            # its body runs — so current_label is correct for that node's very first log line.
            # "updates" still carries the per-node state delta, exactly as before.
            for stream_mode_name, chunk in workflow.stream(initial, config, stream_mode=["updates", "tasks"]):
                if stream_mode_name == "tasks":
                    if "input" not in chunk:
                        continue  # a task RESULT event — nothing here needs it; "updates" has the delta
                    node = chunk.get("name", "")
                    label = _NODE_LABEL.get(node, node)
                    if node == "refactoring" and security_seen:
                        label = "Refactoring (Security Loop)"
                    current_label[0] = label
                    current_label[1] = 0
                    continue

                # stream_mode_name == "updates": chunk is {node_name: partial_state}, one key.
                for node, delta in chunk.items():
                    label = _NODE_LABEL.get(node, node)
                    if node == "refactoring" and security_seen:
                        label = "Refactoring (Security Loop)"

                    if not isinstance(delta, dict):
                        continue

                    # Emit newly-written source files, credited to the node that wrote them — the
                    # Unit Test agent's tests and the Debugging agent's fixes are NOT Code
                    # Generator output.
                    for path in delta.get("generated_code", []) or []:
                        if path not in seen_files:
                            seen_files.add(path)
                            sse_q.put(_sse({"type": "file", "path": path, "agent": label}))
                    for path in delta.get("unit_tests", []) or []:
                        if path not in seen_files:
                            seen_files.add(path)
                            sse_q.put(_sse({"type": "file", "path": path, "kind": "test", "agent": label}))

                    # Repo url (published live).
                    repo_url = delta.get("repo_url")
                    if repo_url and not repo_sent:
                        repo_sent = True
                        sse_q.put(_sse({"type": "repo", "url": repo_url}))

                    # Reports as their paths are written.
                    for key, kind in (
                        ("review_report_path", "code-review"),
                        ("refactoring_report_path", "refactoring"),
                        ("security_report_path", "security"),
                        ("debugging_report_path", "debugging"),
                        ("unit_test_report_path", "unit-test"),
                    ):
                        rp = delta.get(key)
                        if rp and rp not in seen_reports:
                            seen_reports.add(rp)
                            _last_run.setdefault("reports", {})[kind] = rp
                            sse_q.put(_sse({"type": "report", "kind": kind, "path": rp}))

                    # Refactoring's own bookkeeping, captured the moment its node fires — `_last_run
                    # ["state"]` (line ~909, below) isn't filled in until the WHOLE run finishes, so
                    # without this the Edits page (/api/run/refactored-files) would show nothing
                    # until Security/Documentation/etc. have also completed, minutes after
                    # Refactoring itself is done. A later security-loop re-entry overwrites this with
                    # its own (superset) edit list, same as the state field it mirrors.
                    if "refactored_files" in delta:
                        _last_run["refactored_files"] = list(delta.get("refactored_files") or [])
                        _last_run["refactored_code"] = str(delta.get("refactored_code") or "")

                    if node == "security":
                        security_seen = True

                    # Map the node to a frontend stage tile (or a log-only line).
                    stage_id = _NODE_TO_STAGE.get(node, None)
                    if node == "refactoring" and security_seen:
                        stage_id = "refactoring-security-loop"

                    # A conversational turn that actually changed the plan. Announced on its own
                    # event rather than through `log`, which truncates at _MAX_LOG_TEXT and caps
                    # lines per node — and because the interesting payload is the plan diff, which
                    # a log line could not carry. Also refresh plan_count: it was captured once
                    # from the initial plan, so after a patch the dashboard would read "60 of 59".
                    if node == "chat_intake":
                        turns = delta.get("chat_log")
                        # Emit only a turn we have not already announced. chat_intake_node returns
                        # the WHOLE state on every pass — including the no-op passes where nothing
                        # was queued — so `chat_log` stays non-empty for the rest of the run and a
                        # naive `if turns:` re-sent the newest turn on every remaining loop pass
                        # (measured: 59 identical frames for ONE request on a 59-item plan, which
                        # the UI faithfully rendered as 59 rows and 59 notifications).
                        if turns and int(turns[-1].get("seq") or 0) > last_chat_seq:
                            live_plan = delta.get("work_items") or _last_run.get("state", {}).get("work_items") or []
                            _last_run["plan_count"] = len(live_plan)
                            last = turns[-1]
                            last_chat_seq = int(last.get("seq") or 0)
                            sse_q.put(_sse({
                                "type": "chat",
                                "seq": last.get("seq"),
                                "messages": last.get("messages") or [],
                                "report": last.get("report") or {},
                                "diff": last.get("diff") or {},
                                "planCount": len(live_plan),
                            }))

                    if node == "escalate":
                        sse_q.put(_sse({"type": "log", "agent": label, "level": "warn",
                                        "text": "Escalated — needs human review"}))
                    elif stage_id is None:
                        sse_q.put(_sse({"type": "log", "agent": label, "level": "info",
                                        "text": f"{label} completed"}))
                    else:
                        # Only THIS node's own check result may decide pass/fail (_NODE_RESULT_KEY).
                        result = delta.get(_NODE_RESULT_KEY[node]) if node in _NODE_RESULT_KEY else None
                        passed = result.get("passed") if isinstance(result, dict) else None
                        failed = passed is False
                        sse_q.put(_sse({
                            "type": "stage", "node": node, "stageId": stage_id,
                            "status": "failed" if failed else "completed", "label": label,
                        }))
                        # Name what actually failed — otherwise a red tile is the only clue, and
                        # the compile/pytest output the Debugging agent is about to fix stays hidden.
                        for check_name, stderr in _failed_checks(result):
                            sse_q.put(_sse({"type": "log", "agent": label, "level": "warn",
                                            "text": f"{check_name} check FAILED: {stderr or '(no stderr captured)'}"}))
                        sse_q.put(_sse({"type": "log", "agent": label, "level": "warn" if failed else "ok",
                                        "text": f"{label} {'failed' if failed else 'completed'}"}))

            state = workflow.get_state(config).values
            status = state.get("workflow_status", "completed")
            # Keep the finished state so the Output endpoints can report paths, repo/PR urls and
            # the scaffold/test file classification after the stream has closed.
            _last_run["state"] = dict(state)
            _last_run["status"] = status
            _last_run["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            sse_q.put(_sse({
                "type": "done", "status": status,
                "summary": state.get("generation_summary", ""),
                "repo_url": state.get("repo_url", ""),
                "pr_url": state.get("pr_url", ""),
                "package_path": state.get("package_path", ""),
                "files": len(state.get("generated_code", []) or []),
            }))
        except Exception as exc:  # noqa: BLE001 — surface any failure to the UI instead of a dead stream
            logging.exception("run failed")
            # Leave the snapshot in place: whatever the run DID produce before failing is still
            # real output worth showing on the Output pages. Compare identity, not truthiness —
            # _last_run may still hold a PRIOR (different) run's snapshot if this run failed
            # before its own _last_run.clear()/update() below ever ran (e.g. a bad pack name);
            # a bare truthy check would silently mislabel that unrelated, already-finished run as
            # "failed" even though its own artifacts are completely intact on disk.
            if _last_run.get("project") == project:
                _last_run["status"] = "failed"
                _last_run["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            sse_q.put(_sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"}))
        finally:
            # close() releases every reader, including any that attached mid-run — not just the
            # request that launched it.
            _bus.close()
            app_logger.removeHandler(capture)
            if restore_level is not None:
                app_logger.setLevel(restore_level)
            # Un-patch the process-global LLM gateway. MUST happen even when the run raised, or a
            # failed dry-run poisons every subsequent real run in this process.
            if gateway_patch is not None:
                gateway_patch.__exit__(None, None, None)
            _run_lock.release()

    driver = threading.Thread(target=_drive, name=f"run-driver-{project}", daemon=True)
    try:
        driver.start()
    except Exception as exc:  # noqa: BLE001 - couldn't even start the background run (e.g. OS
        # thread-limit pressure): nothing is running yet, so release synchronously right here —
        # this is the "exception before the first yield" case, entirely on the calling thread.
        _bus.close()
        _run_lock.release()
        yield _sse({"type": "error", "message": f"could not start run: {type(exc).__name__}: {exc}"})
        return

    # Just drain this reader for as long as the generator is iterated. If the client disconnects,
    # iteration simply stops here — the run keeps going in the background regardless (see _drive's
    # docstring) and _drive's own finally releases _run_lock/removes the handler when it actually
    # finishes. The only thing to clean up on THIS side is our own queue.
    try:
        while True:
            item = reader.get()
            if item is _DONE:
                break
            yield item
    finally:
        # Drop this reader so the driver stops filling a queue nobody drains. The run continues,
        # and a reconnecting client picks it up from the replay buffer via /api/run/attach.
        _bus.unsubscribe(reader)


def _load_pack(pack_dir: Path) -> dict[str, Any]:
    """Load a pack's top-level artifacts into a name -> content dict (.json parsed, else text)."""
    package: dict[str, Any] = {}
    for path in sorted(pack_dir.iterdir()):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError):
            continue
        if path.suffix == ".json":
            try:
                package[path.name] = json.loads(text)
                continue
            except json.JSONDecodeError:
                pass
        package[path.name] = text
    return package


@app.get("/api/run/stream")
def run_stream(
    pack: str,
    project: str = "frontend-run",
    mode: str = "dry-run",
    publish: bool = False,
    only: str = "",
) -> StreamingResponse:
    """Server-Sent Events: drives the LangGraph pipeline for ``pack`` and streams per-node progress."""
    if mode not in ("dry-run", "real"):
        raise HTTPException(400, f"mode must be 'dry-run' or 'real', got {mode!r}")
    gen = _run_events(pack=pack, project=project, mode=mode, publish=publish, only=only)
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def _attach_events(project: str) -> Iterator[str]:
    """Rejoin the run already in flight (or read back the one that just finished).

    Read-only, and deliberately so: it never touches ``_run_lock`` and never starts a driver. That
    separation is the point — re-opening ``/api/run/stream`` cannot work as a reconnect, because
    that path's whole job is to START a run and the original driver still holds the lock.

    Serves a ``snapshot`` frame first, so the client knows what it has joined and can reset its
    board before replaying:

    * ``status: "running"``  — every frame so far, then the live tail.
    * ``status: "finished"`` — every frame so far, then the stream closes. This is what a browser
      refresh after a completed run gets, and it is why the dashboard can be rebuilt from cold.
    * ``status: "none"``     — nothing to resume for this project. ``current_project`` names what
      the server actually holds, so the UI can say "a different run is in progress" rather than
      spinning forever.

    Replayed frames are the SAME serialized frames the original stream sent, so a client that
    resets and replays lands in exactly the state it would have been in had it never dropped.
    """
    status, kind, frames, reader = _bus.attach(project)
    yield _sse({
        "type": "snapshot",
        "status": status,
        "kind": kind,
        "project": _bus.project,
        "requested_project": project,
        "frames": len(frames),
        "run_status": _last_run.get("status", "") if _last_run.get("project") == _bus.project else "",
    })
    for line in frames:
        yield line
    if reader is None:
        return
    try:
        while True:
            item = reader.get()
            if item is _DONE:
                break
            yield item
    finally:
        _bus.unsubscribe(reader)


@app.get("/api/run/attach")
def run_attach(project: str = "") -> StreamingResponse:
    """Reconnect to a run in progress, or read back the last one. See ``_attach_events``.

    Serves BOTH lanes — the bus is lane-agnostic and reports which one it holds in its ``kind``,
    since only one run can exist at a time process-wide.
    """
    return StreamingResponse(
        _attach_events(project.strip()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/api/run/current")
def run_current() -> dict[str, Any]:
    """What run, if any, this server is holding — a cheap poll for "is there anything to resume?".

    The attach stream answers the same question, but a plain JSON probe is what the app wants on
    boot before deciding whether to open an EventSource at all.
    """
    frames, active = _bus.snapshot_meta()
    return {
        "project": _bus.project,
        "kind": _bus.kind,
        "active": active,
        "frames": frames,
        "status": _last_run.get("status", "") if _last_run.get("project") == _bus.project else "",
        "started_at": _last_run.get("started_at", "") if _last_run.get("project") == _bus.project else "",
        "finished_at": _last_run.get("finished_at", "") if _last_run.get("project") == _bus.project else "",
    }


# ---------------------------------------------------------------- Output pages: real artifacts
#
# Everything below serves what the last (or in-flight) run genuinely produced. Each endpoint
# answers 200 with ``available: false`` + a human-readable ``reason`` when there is nothing yet,
# rather than a 4xx — the frontend renders that as an empty state, not an error.


@app.get("/api/run/files")
def run_files() -> dict[str, Any]:
    """Every file the run wrote, with its Monaco language and scaffold/source/test classification."""
    rec = _last_run
    if not rec.get("project"):
        return {"available": False, "reason": "No run has been started yet.", "count": 0, "files": []}

    executor = rec.get("executor")
    kinds = _file_kinds(rec)
    refactored = set(_refactored_paths(rec))
    root = executor.root if isinstance(executor, LocalDiskExecutor) else None
    memory_files = getattr(executor, "files", {}) if root is None else {}

    files: list[dict[str, Any]] = []
    for path in _run_file_paths(rec):
        if root is not None:
            try:
                size = (root / path).stat().st_size
            except OSError:
                size = 0
        else:
            size = len(str(memory_files.get(path, "")))
        files.append({
            "path": path,
            "language": _language_for(path),
            "size": size,
            "kind": kinds.get(path, "source"),
            # True when the Refactoring agent's write_file tool touched this path this run —
            # independent of "kind" (a refactored file is still a "source" or "test" file).
            "editedByRefactoring": path in refactored,
        })

    return {
        "available": True,
        "project": rec.get("project", ""),
        "mode": rec.get("mode", ""),
        "status": rec.get("status", ""),
        "root": str(root) if root is not None else "(in-memory — dry-run)",
        "count": len(files),
        "files": files,
    }


@app.get("/api/run/refactored-files")
def run_refactored_files() -> dict[str, Any]:
    """Files the Refactoring agent edited this run — a Claude-Code-style "edited files" list.

    Source of truth is ``WorkflowState["refactored_files"]`` (the ``write_file`` tool's own
    record, set in ``app/agents/refactoring.py::execute``), not a guess from git or file mtimes,
    so this reflects exactly what the agent touched regardless of publish/push state. Each entry
    also carries whether the path still exists in the run's current file listing (a since-deleted
    or renamed path would otherwise 404 from ``/api/run/diff``/``/api/run/file``).
    """
    rec = _last_run
    project = str(rec.get("project") or "")
    if not project:
        return {"available": False, "reason": "No run has been started yet.", "count": 0, "files": []}

    current_paths = set(_run_file_paths(rec))
    paths = _refactored_paths(rec)
    # Same live-first preference as _refactored_paths: the summary should match the file list.
    summary = str(rec.get("refactored_code") or (rec.get("state") or {}).get("refactored_code") or "")

    files = [
        {
            "path": path,
            "language": _language_for(path),
            "existsNow": path in current_paths,
        }
        for path in paths
    ]

    return {
        "available": True,
        "project": project,
        "count": len(files),
        "files": files,
        # The agent's own one-line summary of what it fixed/skipped/left unreached this run.
        "summary": summary,
    }


@app.get("/api/run/file")
def run_file(path: str) -> dict[str, Any]:
    """Contents of one generated file. ``path`` must be one this run produced (that check is also
    the traversal guard — nothing outside the run's own output is readable)."""
    rec = _last_run
    executor = rec.get("executor")
    if not executor or not rec.get("project"):
        raise HTTPException(404, "no run has produced files yet")
    if path not in set(_run_file_paths(rec)):
        raise HTTPException(404, f"not a file of the current run: {path!r}")
    try:
        content = executor.read_file(path)
    except (FileNotFoundError, ValueError, OSError, UnicodeDecodeError) as exc:
        raise HTTPException(404, f"could not read {path!r}: {exc}") from exc

    return {
        "path": path,
        "language": _language_for(path),
        "size": len(content),
        "truncated": len(content) > _MAX_FILE_CHARS,
        "content": content[:_MAX_FILE_CHARS],
    }


def _parse_unified_diff(text: str, path: str) -> dict[str, Any] | None:
    """Unified ``git diff`` output -> the frontend's FileDiff shape (hunks of typed lines).

    Returns ``None`` when the diff carries no hunks (identical file / new-and-unchanged).
    """
    hunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    additions = deletions = 0
    old_no = new_no = 0

    for raw in text.splitlines():
        if raw.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if not match:
                continue
            old_no, new_no = int(match.group(1)), int(match.group(2))
            current = {"header": raw, "lines": []}
            hunks.append(current)
        elif current is None or raw.startswith(("+++", "---", "\\")):
            continue  # file headers and "\ No newline at end of file"
        elif raw.startswith("+"):
            current["lines"].append({"type": "add", "oldLine": None, "newLine": new_no, "code": raw[1:]})
            new_no += 1
            additions += 1
        elif raw.startswith("-"):
            current["lines"].append({"type": "del", "oldLine": old_no, "newLine": None, "code": raw[1:]})
            old_no += 1
            deletions += 1
        else:  # context (" prefix", or a bare empty line git emits for a blank context line)
            current["lines"].append({"type": "ctx", "oldLine": old_no, "newLine": new_no, "code": raw[1:]})
            old_no += 1
            new_no += 1

    if not hunks:
        return None
    return {"path": path, "additions": additions, "deletions": deletions, "hunks": hunks}


@app.get("/api/run/diff")
def run_diff(path: str) -> dict[str, Any]:
    """Real ``git diff <base>...<branch>`` for one file — what this run's commits changed.

    The branch pair comes from the run's own state, not from a literal: greenfield works main...dev,
    brownfield works <repo default>...sdlc/cr-<run>. Only meaningful for a real run (dry-run keeps
    files in memory with no git history)."""
    rec = _last_run
    executor = rec.get("executor")
    project = str(rec.get("project") or "")
    if not isinstance(executor, LocalDiskExecutor) or not project:
        return {"available": False, "reason": "Diffs need a real run — dry-run keeps files in memory with no git history."}
    if not (executor.root / project / ".git").is_dir():
        return {"available": False, "reason": "The generated project has no git repository yet."}

    branches = _git_branch_names(executor, project)
    # The run's OWN branch pair when it recorded one, falling back to the greenfield main/dev
    # convention. A brownfield run works on `sdlc/cr-<run>` off whatever the repo's default branch
    # actually is, so a hardcoded main...dev would either diff the wrong pair or claim there is
    # nothing to diff at all.
    state = rec.get("state") or {}
    base = str(state.get("base_branch") or "").strip()
    feature = str(state.get("branch") or "").strip()
    if base not in branches:
        base = next((b for b in ("main", "master") if b in branches), "")
    if feature not in branches:
        feature = "dev" if "dev" in branches else ""
    if not base or not feature or base == feature:
        return {"available": False,
                "reason": f"No branch pair to diff in {project} (branches: {', '.join(branches) or 'none'})."}

    result = executor.run_command(
        ["git", "diff", f"{base}...{feature}", "--", _project_relative(rec, path)], cwd=project
    )
    if result.exit_code != 0:
        return {"available": False, "reason": (result.stderr or "git diff failed").strip()[:200]}
    diff = _parse_unified_diff(result.stdout, path)
    if diff is None:
        return {"available": False, "reason": f"Unchanged between {base} and {feature}."}
    return {"available": True, "base": base, "feature": feature, "diff": diff}


def _md_summary(markdown: str, limit: int = 320) -> str:
    """First prose paragraph of a report — preferring its Executive Summary section."""
    match = re.search(
        r"^#{1,4}\s*(?:Section\s*\d+:\s*)?Executive Summary\s*$\n+(.+?)(?=\n#{1,4}\s|\Z)",
        markdown, re.MULTILINE | re.DOTALL,
    )
    block = match.group(1) if match else markdown
    for paragraph in re.split(r"\n\s*\n", block):
        text = " ".join(line.strip() for line in paragraph.strip().splitlines()).strip()
        if not text or text.startswith(("#", "|", "```", ">", "*", "-")):
            continue
        return text[:limit] + ("…" if len(text) > limit else "")
    return ""


def _locate_report(project: str, *names: str) -> Path | None:
    """Fallback lookup for a report file when the run state hasn't been read back yet.

    The agents write to ``reports/<project>/`` (Security) and ``reports/<project>-<run>/``
    (Code Review + Refactoring); state paths are authoritative, this only covers an in-flight run.
    """
    base = _reports_dir()
    if not base.is_dir():
        return None
    slug = _safe_name(project).lower()
    candidates = [base / slug, base / f"{slug}-{slug}"]
    candidates += sorted(
        (p for p in base.glob(f"{slug}*") if p.is_dir()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for directory in candidates:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def _read_findings(path: Path | None) -> Any:
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


#: kind -> (title, state path field, state inline-content field, sibling findings file)
_REPORT_DEFS: tuple[tuple[str, str, str, str, str], ...] = (
    ("code-review", "Code Review Report", "review_report_path", "review_report", "report.md"),
    ("refactoring", "Refactoring Report", "refactoring_report_path", "refactoring_report", "refactoring-report.md"),
    ("security", "Security Review Report", "security_report_path", "security_report", "security-report.md"),
    ("debugging", "Debugging Report", "debugging_report_path", "debugging_report", "debugging-report.md"),
    ("unit-test", "Unit Test Generation Report", "unit_test_report_path", "unit_test_report", "unit-test-report.md"),
)


@app.get("/api/run/reports")
def run_reports() -> dict[str, Any]:
    """The Markdown the Code Review / Refactoring / Security / Documentation agents actually wrote."""
    rec = _last_run
    project = str(rec.get("project") or "")
    if not project:
        return {"available": False, "reason": "No run has been started yet.", "reports": []}

    state = rec.get("state") or {}
    streamed = rec.get("reports") or {}
    reports: list[dict[str, Any]] = []

    for kind, title, path_field, content_field, filename in _REPORT_DEFS:
        raw_path = state.get(path_field) or streamed.get(kind) or ""
        path = Path(raw_path) if raw_path else _locate_report(project, filename)
        content = ""
        if path is not None and path.is_file():
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                content = ""
        if not content:
            content = str(state.get(content_field) or "")
        if not content:
            continue  # this agent hasn't produced its report yet — don't invent a card for it

        status = "ok"
        if kind == "code-review":
            findings = _read_findings(path.parent / "findings.json" if path else None)
            if isinstance(findings, list) and findings:
                status = "warning"
        elif kind == "security":
            payload = _read_findings(path.parent / "security-findings.json" if path else None)
            if isinstance(payload, dict):
                items = payload.get("findings") or []
                severities = {str(f.get("severity", "")).lower() for f in items if isinstance(f, dict)}
                if severities & {"critical", "high"}:
                    status = "critical"
                elif items or payload.get("verdict") == "changes_requested":
                    status = "warning"

        reports.append({
            "id": f"report-{kind}",
            "kind": kind,
            "title": title,
            "path": str(path) if path else "",
            "generatedAt": _iso_mtime(path) if path else "",
            "status": status,
            "summary": _md_summary(content),
            "content": content,
        })

    documentation = str(state.get("documentation") or "")
    if documentation:
        reports.append({
            "id": "report-documentation",
            "kind": "documentation",
            "title": "Documentation",
            "path": "",
            "generatedAt": "",
            "status": "ok",
            "summary": _md_summary(documentation),
            "content": documentation,
        })

    return {"available": True, "project": project, "count": len(reports), "reports": reports}


_VALID_SEVERITIES = ("critical", "high", "medium", "low")


def _normalize_finding(index: int, raw: dict[str, Any]) -> dict[str, Any]:
    """One Semgrep-shaped finding -> the shape the Security page's components consume."""
    severity = str(raw.get("severity", "")).strip().lower()
    if severity not in _VALID_SEVERITIES:
        severity = "low"
    message = str(raw.get("message") or raw.get("description") or "").strip()
    rule = str(raw.get("rule") or raw.get("check_id") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not title:
        # Semgrep messages are a sentence or two; the first sentence makes a usable headline and
        # the full message still shows as the description.
        title = message.split(". ")[0].strip()[:120] if message else (rule.rsplit(".", 1)[-1] or "Finding")
    try:
        line = int(raw.get("line") or 0)
    except (TypeError, ValueError):
        line = 0
    return {
        "id": str(raw.get("id") or rule or f"FINDING-{index + 1}"),
        "severity": severity,
        "title": title,
        "description": message or "No description was reported by the scanner.",
        "recommendation": str(raw.get("recommendation") or raw.get("fix") or "").strip()
                          or "See the Security Review report for remediation guidance.",
        "file": str(raw.get("file") or raw.get("path") or ""),
        "line": line,
        # The Security<->Refactoring loop re-scans after each fix, so whatever survives into the
        # final findings file is still open. Nothing here is inferred as "fixed".
        "status": str(raw.get("status") or "open").strip().lower(),
        "rule": rule,
    }


@app.get("/api/run/security")
def run_security() -> dict[str, Any]:
    """The Security agent's real verdict, executive summary and normalized findings."""
    rec = _last_run
    project = str(rec.get("project") or "")
    if not project:
        return {"available": False, "reason": "No run has been started yet.", "findings": []}

    state = rec.get("state") or {}
    raw_path = state.get("security_findings_path") or ""
    findings_path = Path(raw_path) if raw_path else _locate_report(project, "security-findings.json")
    payload = _read_findings(findings_path)
    if not isinstance(payload, dict):
        return {
            "available": False,
            "reason": "The Security Review stage hasn't produced findings for this run yet.",
            "findings": [],
        }

    raw_findings = [f for f in (payload.get("findings") or []) if isinstance(f, dict)]
    findings = [_normalize_finding(i, f) for i, f in enumerate(raw_findings)]
    counts = {severity: sum(1 for f in findings if f["severity"] == severity) for severity in _VALID_SEVERITIES}

    report_path = state.get("security_report_path") or ""
    report = Path(report_path) if report_path else _locate_report(project, "security-report.md")

    return {
        "available": True,
        "project": project,
        "verdict": str(payload.get("verdict") or state.get("security_verdict") or ""),
        "summary": str(payload.get("summary") or ""),
        "findingsPath": str(findings_path) if findings_path else "",
        "reportPath": str(report) if report else "",
        "generatedAt": _iso_mtime(findings_path) if findings_path else "",
        "counts": counts,
        "total": len(findings),
        "findings": findings,
    }


def _git_branch_names(executor: LocalDiskExecutor, project: str) -> list[str]:
    result = executor.run_command(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=project)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()] if result.exit_code == 0 else []


def _feature_id(subject: str) -> str:
    """Conventional-commit scope, e.g. ``feat(F-03): ...`` -> ``F-03``."""
    match = re.match(r"^\w+\(([^)]{1,40})\)\s*:", subject)
    return match.group(1) if match else ""


@app.get("/api/run/github")
def run_github() -> dict[str, Any]:
    """The generated repository's real state: branches, commit history, remote and PR status.

    Read straight out of the project's own git repo (plus the run state's ``repo_url``/``pr_url``),
    so it reflects the commits the pipeline actually made rather than a canned history.
    """
    rec = _last_run
    project = str(rec.get("project") or "")
    state = rec.get("state") or {}
    executor = rec.get("executor")

    if not project:
        return {"available": False, "reason": "No run has been started yet."}
    if not isinstance(executor, LocalDiskExecutor):
        return {
            "available": False,
            "reason": "Dry-run keeps everything in memory — no git repository is created. "
                      "Launch a run in Real mode to see branches, commits and the pull request.",
        }
    repo_dir = executor.root / project
    if not (repo_dir / ".git").is_dir():
        return {"available": False, "reason": f"No git repository at {repo_dir} yet."}

    def git(*args: str) -> str:
        result = executor.run_command(["git", *args], cwd=project)
        return result.stdout.strip() if result.exit_code == 0 else ""

    branch_names = _git_branch_names(executor, project)
    current_branch = git("rev-parse", "--abbrev-ref", "HEAD")
    default_branch = next((b for b in ("main", "master") if b in branch_names), current_branch)
    feature_branch = "dev" if "dev" in branch_names else ""

    branches: list[dict[str, Any]] = []
    for name in branch_names:
        info = git("log", "-1", "--format=%h%x1f%aI%x1f%s", name).split("\x1f")
        count = git("rev-list", "--count", name)
        branches.append({
            "name": name,
            "sha": info[0] if info and info[0] else "",
            "lastCommitAt": info[1] if len(info) > 1 else "",
            "lastCommitMessage": info[2] if len(info) > 2 else "",
            "commits": int(count) if count.isdigit() else 0,
            "isDefault": name == default_branch,
            "isCurrent": name == current_branch,
        })

    # Walk the default branch first so the shared scaffold commit is attributed to it rather than
    # to dev (which contains it as an ancestor) — matching how the pipeline actually builds them.
    ordered = sorted(branch_names, key=lambda n: (n != default_branch, n != feature_branch, n))
    commits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in ordered:
        for line in git("log", name, "--max-count=100", "--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s").splitlines():
            parts = line.split("\x1f")
            if len(parts) != 5 or parts[0] in seen:
                continue
            seen.add(parts[0])
            commits.append({
                "sha": parts[1], "fullSha": parts[0], "author": parts[2],
                "timestamp": parts[3], "message": parts[4],
                "branch": name, "featureId": _feature_id(parts[4]),
            })
    commits.sort(key=lambda c: c["timestamp"], reverse=True)

    remote = git("remote", "get-url", "origin")
    repo_url = str(state.get("repo_url") or "")
    if not repo_url and remote.startswith("http"):
        repo_url = remote[:-4] if remote.endswith(".git") else remote
    slug_match = re.search(r"github\.com[:/]+([^/]+/[^/.]+)", repo_url or remote or "")
    repository = slug_match.group(1) if slug_match else project

    publishing = bool(rec.get("publish"))
    if repo_url:
        push_status = "pushed"
    elif not publishing:
        push_status = "local"       # committed locally on purpose — publishing wasn't requested
    else:
        push_status = "pending" if rec.get("status") == "running" else "failed"

    pr_url = str(state.get("pr_url") or "")
    finalize_status = str(state.get("finalize_status") or "")
    pr_status = "open" if pr_url else ("failed" if finalize_status == "pr_failed" else "not-opened")

    return {
        "available": True,
        "project": project,
        "repository": repository,
        "url": repo_url,
        "localPath": str(repo_dir),
        "visibility": ("public" if publishing else "private") if repo_url else "local",
        "defaultBranch": default_branch,
        "featureBranch": feature_branch,
        "currentBranch": current_branch,
        "headSha": git("rev-parse", "--short", "HEAD"),
        "pushStatus": push_status,
        "prStatus": pr_status,
        "prUrl": pr_url,
        "finalizeStatus": finalize_status,
        "branches": branches,
        "commits": commits,
    }


# ---------------------------------------------------------------- run metrics (dashboard tiles)
#
# The Dashboard's "Run Metrics" grid used to render "—" for most tiles because the SSE stream only
# carried a handful of numbers. This endpoint computes the rest from the SAME real artifacts the
# other Output endpoints read — generated files (lines of code), the test list, the Security and
# Code Review findings JSON, and the generated repo's git log — so every tile shows a real value
# (still "—"/null only when a number genuinely doesn't exist yet, e.g. commits in dry-run).


_BINARY_EXT_FOR_LOC = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".pdf", ".zip", ".gz", ".tar",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp4", ".mp3", ".wav", ".mov", ".exe", ".bin",
})


def _run_loc(rec: dict[str, Any]) -> int:
    """Total lines across the run's generated text files (read through the executor).

    Binary assets are skipped by extension and anything that fails to decode as text is skipped
    too, so an SVG/PNG never inflates or breaks the count.
    """
    executor = rec.get("executor")
    if executor is None:
        return 0
    total = 0
    for path in _run_file_paths(rec):
        if any(path.lower().endswith(ext) for ext in _BINARY_EXT_FOR_LOC):
            continue
        try:
            content = executor.read_file(path)
        except (FileNotFoundError, ValueError, OSError, UnicodeDecodeError):
            continue
        if content:
            total += content.count("\n") + (0 if content.endswith("\n") else 1)
    return total


def _review_findings_count(rec: dict[str, Any]) -> int | None:
    """How many findings the Code Review agent recorded (its normalized findings.json)."""
    state = rec.get("state") or {}
    raw = state.get("review_findings_path") or ""
    path = Path(raw) if raw else _locate_report(str(rec.get("project") or ""), "findings.json")
    data = _read_findings(path)
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):  # some renderers wrap the list
        items = data.get("findings")
        return len(items) if isinstance(items, list) else None
    return None


def _security_findings_count(rec: dict[str, Any]) -> int | None:
    state = rec.get("state") or {}
    raw = state.get("security_findings_path") or ""
    path = Path(raw) if raw else _locate_report(str(rec.get("project") or ""), "security-findings.json")
    data = _read_findings(path)
    if isinstance(data, dict):
        if isinstance(data.get("findings_count"), int):
            return data["findings_count"]
        items = data.get("findings")
        return len(items) if isinstance(items, list) else None
    if isinstance(data, list):
        return len(data)
    return None


def _commit_count(rec: dict[str, Any]) -> int | None:
    """Total commits in the generated repo across all branches (real mode only)."""
    executor = rec.get("executor")
    project = str(rec.get("project") or "")
    if not isinstance(executor, LocalDiskExecutor) or not project:
        return None
    if not (executor.root / project / ".git").is_dir():
        return None
    result = executor.run_command(["git", "rev-list", "--all", "--count"], cwd=project)
    out = result.stdout.strip()
    return int(out) if result.exit_code == 0 and out.isdigit() else None


def _execution_seconds(rec: dict[str, Any]) -> float | None:
    """Wall-clock seconds from run start to finish (None while still running / if unknown)."""
    started, finished = rec.get("started_at"), rec.get("finished_at")
    if not started or not finished:
        return None
    try:
        return (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
    except (ValueError, TypeError):
        return None


def _tests_status(state: dict[str, Any]) -> str:
    """passed / failed / unknown from the Unit-Test phase's fixed-check result."""
    result = state.get("test_result")
    if isinstance(result, dict) and "passed" in result:
        return "passed" if result["passed"] else "failed"
    if "tests_ok" in state:
        return "passed" if state.get("tests_ok") else "failed"
    return "unknown"


@app.get("/api/run/metrics")
def run_metrics() -> dict[str, Any]:
    """Every Dashboard metric tile, computed from the run's real artifacts + final state."""
    rec = _last_run
    if not rec.get("project"):
        return {"available": False, "reason": "No run has been started yet."}

    state = rec.get("state") or {}
    status = str(rec.get("status") or "")
    files = _run_file_paths(rec)
    retry_breakdown = {
        "repair": int(state.get("repair_attempt") or 0),
        "debug": int(state.get("debug_attempt") or 0),
        "security": int(state.get("security_loop_attempt") or 0),
    }

    return {
        "available": True,
        "project": rec.get("project", ""),
        "mode": rec.get("mode", ""),
        "status": status,
        "filesGenerated": len(files),
        "linesOfCode": _run_loc(rec),
        "workItems": int(rec.get("plan_count") or len(state.get("work_items") or []) or 0),
        "testsGenerated": len(state.get("unit_tests") or []),
        "testsStatus": _tests_status(state),
        "securityFindings": _security_findings_count(rec),
        "reviewFindings": _review_findings_count(rec),
        "commits": _commit_count(rec),
        "prStatus": ("open" if state.get("pr_url")
                     else "failed" if state.get("finalize_status") == "pr_failed"
                     else "not-opened"),
        "executionSeconds": _execution_seconds(rec),
        # A completed run is a 100% success; anything else has no meaningful single percentage yet.
        "successRate": 100 if status == "completed" else None,
        "retryLoops": sum(retry_breakdown.values()),
        "retryBreakdown": retry_breakdown,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="IMP-001 LangGraph HTTP/SSE bridge for the frontend")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(f"IMP-001 graph bridge -> http://{args.host}:{args.port}")
    print("  GET /api/packs | POST /api/plan | GET /api/run/stream?pack=<name>&mode=dry-run")
    print("  output: /api/run/files | /api/run/file?path= | /api/run/diff?path= |"
          " /api/run/reports | /api/run/security | /api/run/github | /api/run/metrics")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
