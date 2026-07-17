r"""Local demo server + chat UI for the IMP-001 code-generation agent.

Two modes for the SAME real LangGraph pipeline (scaffold → plan → generate → completeness gate
→ batch-review/HITL → commit):

* DRY-RUN (default): a FakeExecutor holds files in memory and canned replies stand in for Claude,
  so it starts with NO Docker, NO API key. Good for watching the flow; file bodies are stubs.

    python scripts/demo_server.py

* REAL (--real): uses the real LLM gateway (real Claude via Foundry — needs
  ANTHROPIC_FOUNDRY_API_KEY + endpoint in .env) and a LocalDiskExecutor that writes generated
  files to a real folder and makes a real local git commit on approval (NO push). No Docker
  needed — the completeness-only gate means nothing is compiled/built.

    python scripts/demo_server.py --real
    python scripts/demo_server.py --real --out-dir C:\path\to\generated

Open http://127.0.0.1:8100 and drive the agent as a chat.

This is a DEV DEMO harness, deliberately kept OUT of app/api/routes.py: it swaps the executor /
patches the gateway per process. Fine for a single-user local demo; not how the production
service runs (which uses the sandboxed MCPExecutor).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

_IMPL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_IMPL_DIR))  # so `app.*` imports resolve

from app.config.settings import get_settings  # noqa: E402
from app.graph.graph import workflow  # noqa: E402
from app.graph.state import new_state  # noqa: E402
from app.integrations.executor import Executor, FakeExecutor, set_executor  # noqa: E402
from app.services import design_pack  # noqa: E402
from app.services import llm_gateway  # noqa: E402
from app.services.plan_builder import build_plan  # noqa: E402
from scripts.local_executor import LocalDiskExecutor  # noqa: E402
from app.services.boilerplate import render_scaffold  # noqa: E402
from scripts import feature_commit as fc  # noqa: E402  (feature-wise generation helpers)

def _find_repo_root(start: Path) -> Path:
    """Repo root = nearest ancestor (including ``start``) that contains a ``fixtures/`` dir.

    Robust to layout: works whether the service lives at ``<root>/SDLC_Implementation``
    (standalone) or ``<root>/services/implementation`` (monorepo). Falls back to ``start``'s
    parent when no ``fixtures/`` is found, preserving the previous default shape.
    """
    for candidate in (start, *start.parents):
        if (candidate / "fixtures").is_dir():
            return candidate
    return start.parent


_REPO_ROOT = _find_repo_root(_IMPL_DIR)
_FIXTURES_DIR = _REPO_ROOT / "fixtures"
_UI_FILE = Path(__file__).resolve().parent / "demo_ui.html"

# Set by main(): "dry-run" | "real", and where real-mode files are written. Generated projects go
# OUTSIDE the SDLC repo (fc._DEFAULT_OUT_DIR) so their per-project git repos never nest in the main
# tree; override with --out-dir.
MODE = "dry-run"
OUT_DIR = fc._DEFAULT_OUT_DIR


# --------------------------------------------------------------------------- canned LLM (dry run)

def _stub_content(path: str) -> str:
    """A small, believable file body so the demo shows more than "# placeholder"."""
    name = path.rsplit("/", 1)[-1]
    ident = re.sub(r"[^A-Za-z0-9]", "_", name.split(".")[0])
    if path.endswith(".tsx"):
        return f"export default function {ident}() {{\n  return <div className=\"{ident}\">{ident}</div>;\n}}\n"
    if path.endswith(".ts"):
        return f"// {name} (demo stub)\nexport const {ident} = async () => {{\n  // TODO: implement\n}};\n"
    if path.endswith(".py"):
        return f'"""{name} — demo stub."""\n\n\ndef handler():\n    raise NotImplementedError\n'
    return f"// {name} — demo stub\n"


def _canned_reply(prompt: str, **_kw: Any) -> str:
    """Dry-run LLM: for codegen return the requested target files; for repair echo them back."""
    codegen = re.search(r"Target files \(produce ONLY these\):\n((?:- .+\n?)*)", prompt)
    if codegen:
        paths = [ln[2:].strip() for ln in codegen.group(1).splitlines() if ln.startswith("- ")]
        paths = [p for p in paths if p and p != "(none specified)"]
        files = [{"path": p, "content": _stub_content(p)} for p in paths]
        return json.dumps({"files": files or [{"path": "placeholder.txt", "content": "# placeholder\n"}], "notes": "demo"})

    feedback = ""
    fb = re.search(r"Captured stderr:\n(.+?)\n\n", prompt, re.DOTALL)
    if fb:
        feedback = fb.group(1).strip()
    blocks = re.findall(r"### (\S+)\n(.*?)(?=\n### |\Z)", prompt, re.DOTALL)
    files = [
        {"path": p, "content": c.rstrip() + f"\n// reworked per review: {feedback}\n"}
        for p, c in blocks
    ]
    return json.dumps({"files": files or [{"path": "placeholder.txt", "content": "# reworked\n"}], "notes": "demo-rework"})


def _install_canned_gateway() -> None:
    """Dry-run only: replace the real Claude gateway with canned replies (called from main())."""
    llm_gateway.llm_gateway.complete = _canned_reply                              # type: ignore[method-assign]
    llm_gateway.llm_gateway.complete_with_tools = lambda prompt, **kw: _canned_reply(prompt)  # type: ignore[method-assign]


def _make_executor(run_id: str) -> Executor:
    """FakeExecutor (in-memory) for dry-run; LocalDiskExecutor (real files + git) for --real."""
    return LocalDiskExecutor(OUT_DIR) if MODE == "real" else FakeExecutor()


# --------------------------------------------------------------------------- run state + snapshot

# run_id -> {"executor": FakeExecutor, "config": dict, "item_files": {id: [paths]}}
RUNS: dict[str, dict[str, Any]] = {}


def _snapshot(run_id: str, state: dict[str, Any]) -> dict[str, Any]:
    """Turn the run's generation_summary + generated_code into structured chat events."""
    summary = state.get("generation_summary", "")
    generated = list(state.get("generated_code", []))
    items: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    scaffold: dict[str, Any] = {"text": "", "files": []}

    def _item(iid: str) -> dict[str, Any]:
        if iid not in items:
            items[iid] = {"id": iid, "plan": "", "files": [], "failed": False}
            order.append(iid)
        return items[iid]

    for raw in summary.splitlines():
        line = raw.strip()
        if line.startswith("[scaffold]"):
            scaffold["text"] = line[len("[scaffold]"):].strip()
        elif line.startswith("[plan]"):
            m = re.match(r"\[plan\] ([^:]+): (.*)", line)
            if m:
                _item(m.group(1))["plan"] = m.group(2)
        elif line.startswith("[code_generator]"):
            m = re.match(r"\[code_generator\] ([^:]+): (.*)", line)
            if m:
                it = _item(m.group(1))
                rest = m.group(2)
                files_m = re.search(r"\[([^\]]*)\]", rest)
                if files_m and files_m.group(1).strip():
                    it["files"] = [p.strip() for p in files_m.group(1).split(",") if p.strip()]
                if "FAILED" in rest:
                    it["failed"] = True
                    it["detail"] = rest

    item_files = {f for it in items.values() for f in it["files"]}
    scaffold["files"] = [p for p in generated if p not in item_files]

    events: list[dict[str, Any]] = []
    if scaffold["files"] or scaffold["text"]:
        events.append({"kind": "scaffold", **scaffold})
    for iid in order:
        events.append({"kind": "item", **items[iid]})

    if run_id in RUNS:
        RUNS[run_id]["item_files"] = {it["id"]: it["files"] for it in items.values()}

    return {
        "run_id": run_id,
        "status": state.get("workflow_status"),
        "file_count": len(generated),
        "item_count": len(order),
        "events": events,
    }


# --------------------------------------------------------------------------- API

app = FastAPI(title="IMP-001 code-generator demo")

# The React frontend (Vite) runs on its own origin — allow it to call this backend.
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


class RunRequest(BaseModel):
    pack: str
    project: str = "app"  # dir under OUT_DIR + what publish targets
    only: str = ""  # optional substring filter on work-item ids


class ReviewRequest(BaseModel):
    run_id: str
    approved: bool
    rejections: dict[str, str] = {}


class PlanRequest(BaseModel):
    pack: str = "ecommerce_complete"
    only: str = ""  # optional substring filter on work-item ids (e.g. "login")


class PublishRequest(BaseModel):
    repoName: str = ""  # blank → the agent suggests a name from the built app
    visibility: str = "public"  # "private" | "public" (the demo publishes public by default)
    owner: str = ""  # blank → GITHUB_OWNER from .env, else the token's own account
    project: str = "ecommerce"  # which generated project dir to publish (under OUT_DIR)
    token: str = ""  # optional PAT override; blank → GITHUB_PAT from .env


class SuggestNameRequest(BaseModel):
    project: str = "app"  # generated project dir the agent names a repo for


class RunFeatureRequest(BaseModel):
    """One user story of a feature-wise run. The UI calls this once per story (index 0..N-1)."""

    pack: str
    project: str = "app"
    index: int = 0
    repoName: str = ""
    owner: str = ""  # blank → authenticated account
    visibility: str = "private"  # "private" | "public"
    push: bool = False
    reset: bool = True  # on index 0, start the project dir fresh (clean feature history)


def _gh(*args: str, token: str = "") -> tuple[int, str, str]:
    """Run a gh CLI command; return (exit_code, stdout, stderr).

    When ``token`` is given, run gh AS that Personal Access Token's owner by setting
    ``GH_TOKEN``/``GITHUB_TOKEN`` for this call — gh then ignores the keyring login, so
    auth/owner/name checks all reflect the token's account (not whoever gh is logged into).
    """
    env = {**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token} if token else None
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120, env=env)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return 127, "", "gh CLI not found"
    except subprocess.TimeoutExpired:
        return 124, "", "gh timed out"


def _env_token() -> str:
    """The publish PAT from .env (GITHUB_PAT). Blank → fall back to the gh keyring login."""
    return (get_settings().github_pat or "").strip()


def _env_owner() -> str:
    """The account/org to own the repo (GITHUB_OWNER). Blank → the token's own login."""
    return (get_settings().github_owner or "").strip()


def _slug(text: str) -> str:
    """A safe kebab-case GitHub repo name fragment."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s[:60] or "app"


def _suggest_repo_name(project: str) -> str:
    """Agent-suggested repo name for the app that was built.

    Asks the LLM for a short, descriptive kebab-case name using the generated file list as
    context; falls back to a slug of the project folder name if the LLM is unavailable.
    """
    fallback = _slug(project)
    proj_dir = OUT_DIR / project
    files: list[str] = []
    if proj_dir.exists():
        for p in sorted(proj_dir.rglob("*")):
            if p.is_file() and ".git" not in p.parts:
                files.append(str(p.relative_to(proj_dir)).replace("\\", "/"))
            if len(files) >= 40:
                break
    try:
        system = (
            "You name GitHub repositories. Reply with ONLY a repository name in kebab-case "
            "(lowercase letters, digits and hyphens; 2-4 words; no spaces, quotes or explanation)."
        )
        prompt = (
            f"Project folder: {project}\n"
            "Files produced:\n" + ("\n".join(f"- {f}" for f in files) or "- (none)")
            + "\n\nSuggest one concise, descriptive repository name for this application."
        )
        raw = llm_gateway.llm_gateway.complete(prompt=prompt, system=system, max_tokens=40)
        first = (raw or "").strip().splitlines()[0] if raw else ""
        return _slug(first) or fallback
    except Exception as exc:  # noqa: BLE001 - naming must never block a publish
        print(f"[suggest-name] LLM failed ({type(exc).__name__}): {exc}; using {fallback!r}", flush=True)
        return fallback


def _unique_repo_name(owner: str, name: str, token: str) -> str:
    """Return a name not already taken under ``owner`` (append -2, -3, … if it exists)."""
    if not owner:
        return name
    candidate, n = name, 1
    while _gh("repo", "view", f"{owner}/{candidate}", token=token)[0] == 0:
        n += 1
        candidate = f"{name}-{n}"
        if n > 50:
            break
    return candidate


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _UI_FILE.read_text(encoding="utf-8")


@app.get("/api/mode")
def mode() -> dict[str, str]:
    """Which mode the server is in, so the UI can label it (dry-run vs real Claude)."""
    return {"mode": MODE, "out_dir": str(OUT_DIR) if MODE == "real" else ""}


@app.get("/api/accounts")
def accounts() -> dict[str, Any]:
    """GitHub accounts the gh CLI is logged into — for the 'publish as' picker in the form."""
    code, active, _ = _gh("api", "user", "--jq", ".login")
    active = active if code == 0 else ""
    status = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    logins = re.findall(r"account (\S+)", (status.stdout or "") + (status.stderr or ""))
    accts = sorted({*logins, *([active] if active else [])})
    return {"active": active, "accounts": accts or ([active] if active else [])}


def _resolve_pack_dir(pack: str) -> Path | None:
    """Accept a design pack as a name under fixtures/ OR a full/relative directory path.

    A valid pack is any directory whose artifacts identify a design package by CONTENT
    (an OpenAPI spec or a UI↔API mapping table) — filenames/extensions don't matter.
    """
    raw = pack.strip()
    if not raw:
        return None
    cand = Path(raw)
    candidates = [cand] if cand.is_absolute() else [_FIXTURES_DIR / raw, _REPO_ROOT / raw, cand]
    for c in candidates:
        try:
            if c.is_dir() and design_pack.is_design_pack(c):
                return c
        except OSError:
            continue
    return None


@app.post("/api/plan")
def plan(req: PlanRequest) -> dict[str, Any]:
    """Deterministic plan (no LLM): decompose the design pack into work items.

    ``pack`` may be a fixtures name OR a path to your own design-package folder. Artifacts are
    identified by CONTENT (see :mod:`app.services.design_pack`), so filenames/extensions vary
    freely — the folder just needs an API surface (an OpenAPI spec or a UI↔API mapping table).
    """
    pack_dir = _resolve_pack_dir(req.pack)
    if pack_dir is None:
        raise HTTPException(
            404,
            f"no design package found for {req.pack!r} — expected a fixtures/ name or a folder "
            "path whose contents include an API surface (an OpenAPI spec or a UI↔API mapping "
            "table); a schema (SQL or JSON) and structure files are used when present.",
        )
    items = build_plan(pack_dir)
    if req.only:
        needle = req.only.lower()
        items = [w for w in items if needle in w.id.lower()]
    return {
        "count": len(items),
        "items": [
            {
                "id": w.id,
                "requirement_ids": w.requirement_ids,
                "endpoints": w.endpoints,
                "tables": w.tables,
                "screens": w.screens,
                "target_files": w.target_files,
            }
            for w in items
        ],
    }


def _generate_narrative(items: list) -> str:
    """Ask Claude for a human-readable implementation plan (markdown) from the work items."""
    eps = sorted({e for w in items for e in w.endpoints})
    tables = sorted({t for w in items for t in w.tables})
    screens = sorted({s for w in items for s in w.screens})
    lines = []
    for w in items:
        extra = []
        if w.endpoints:
            extra.append("endpoints " + ", ".join(w.endpoints))
        if w.screens:
            extra.append("screens " + ", ".join(w.screens))
        if w.tables:
            extra.append("tables " + ", ".join(w.tables))
        lines.append(f"- {w.id}: produces {', '.join(w.target_files)}" + ("; " + "; ".join(extra) if extra else ""))

    system = (
        "You are a senior engineer writing an implementation plan for a fellow developer to SKIM "
        "and approve in under a minute. Output GitHub-flavored markdown optimized for fast "
        "reading — NOT dense prose. Follow this shape exactly:\n"
        "# <short title>\n"
        "One or two plain sentences on what will be built.\n\n"
        "## Tech stack\n"
        "- 3-5 short bullets (FastAPI backend, React + TypeScript frontend, etc.)\n\n"
        "## What gets built\n"
        "For EACH work item, a '### <work-item id>' followed by:\n"
        "- **What:** one line\n"
        "- **How:** 1-2 short bullets\n"
        "- **Files:** the target files as bullets using `path` in backticks\n\n"
        "## Key decisions & assumptions\n"
        "- short bullets\n\n"
        "## Risks / things to watch\n"
        "- short bullets\n\n"
        "Rules: keep every bullet to one line; bold key terms; put file names / endpoints / tables "
        "in `backticks`; no paragraphs longer than 2 sentences; do NOT write code."
    )
    prompt = (
        "Design package summary:\n"
        f"- API endpoints: {', '.join(eps) or '-'}\n"
        f"- Database tables: {', '.join(tables) or '-'}\n"
        f"- UI screens: {', '.join(screens) or '-'}\n\n"
        "Deterministic work-item breakdown to implement:\n"
        + "\n".join(lines)
        + "\n\nWrite the implementation plan now."
    )
    return llm_gateway.llm_gateway.complete(prompt=prompt, system=system, max_tokens=2000)


@app.post("/api/plan-narrative")
def plan_narrative(req: PlanRequest) -> dict[str, Any]:
    """Deterministic work items + a Claude-authored implementation plan (markdown) to read first."""
    pack_dir = _resolve_pack_dir(req.pack)
    if pack_dir is None:
        raise HTTPException(404, f"no design package found for {req.pack!r}")
    items = build_plan(pack_dir)
    if req.only:
        needle = req.only.lower()
        items = [w for w in items if needle in w.id.lower()]

    narrative = ""
    try:
        narrative = _generate_narrative(items)
    except Exception as exc:  # noqa: BLE001 - fall back to the item breakdown if the LLM call fails
        print(f"[plan-narrative] LLM call failed: {type(exc).__name__}: {exc}", flush=True)

    return {
        "count": len(items),
        "narrative": narrative,
        "items": [
            {
                "id": w.id,
                "requirement_ids": w.requirement_ids,
                "endpoints": w.endpoints,
                "tables": w.tables,
                "screens": w.screens,
                "target_files": w.target_files,
            }
            for w in items
        ],
    }


@app.get("/api/packs")
def packs() -> dict[str, list[str]]:
    """Design-pack directories under fixtures/ that plan_builder can decompose."""
    found = []
    if _FIXTURES_DIR.is_dir():
        for d in sorted(_FIXTURES_DIR.iterdir()):
            if d.is_dir() and design_pack.is_design_pack(d):
                found.append(d.name)
    return {"packs": found}


@app.post("/api/run")
def run(req: RunRequest) -> dict[str, Any]:
    """Run the REAL code-generator agent graph (scaffold → generate → gate → auto-commit).

    Writes to generated/<project> (so publish targets the same folder). In real mode this uses
    the real Claude gateway (ANTHROPIC_FOUNDRY_API_KEY). Runs to completion with no human-in-the-
    loop: a completed plan auto-commits; a repair-cap failure ends flagged needs_human_review.
    """
    pack_dir = _resolve_pack_dir(req.pack)
    if pack_dir is None:
        raise HTTPException(404, f"no design package found for {req.pack!r}")

    design_package = _load_pack(pack_dir)
    work_items = build_plan(pack_dir)
    if req.only:
        needle = req.only.lower()
        work_items = [w for w in work_items if needle in w.id.lower()]

    project = req.project  # files land in generated/<project>; publish uses the same
    thread = f"{project}-{len(RUNS) + 1}"  # unique checkpointer key per run
    executor = _make_executor(project)
    config = {"configurable": {"thread_id": thread}, "recursion_limit": 1000}
    RUNS[thread] = {"executor": executor, "config": config, "item_files": {}}

    set_executor(executor)
    initial = new_state(
        run_id=thread, attempt=0, project_id=project,
        design_package=design_package, work_items=work_items,
    )
    try:
        workflow.invoke(initial, config)
    except Exception as exc:  # noqa: BLE001 - surface LLM/exec errors to the chat instead of a 500 page
        raise HTTPException(502, f"run failed ({type(exc).__name__}): {exc}") from exc
    snap = _snapshot(thread, workflow.get_state(config).values)
    snap["project"] = project
    snap["out_dir"] = str(OUT_DIR / project)
    return snap


@app.post("/api/review")
def review(req: ReviewRequest) -> dict[str, Any]:
    """No-op kept for UI compatibility: human review was removed, so /api/run already committed.

    There is no interrupt to resume — this just returns the finished run's snapshot so older UIs
    that still call it keep working.
    """
    run = RUNS.get(req.run_id)
    if run is None:
        raise HTTPException(404, f"no active run {req.run_id!r}")
    snap = _snapshot(req.run_id, workflow.get_state(run["config"]).values)
    snap["reworked"] = {}
    if MODE == "real":
        snap["out_dir"] = str(OUT_DIR / req.run_id)
    return snap


@app.post("/api/publish")
def publish(req: PublishRequest) -> dict[str, Any]:
    """REALLY create a GitHub repo for the generated project and push (via the gh CLI).

    Pre-checks map 1:1 to the frontend's failure states so the UI reflects the *real* outcome:
    auth-failed / invalid-owner / repo-exists / (generate-missing) / success. Creating the repo
    is a genuine outward-facing action on the authenticated account.
    """
    token = req.token.strip() or _env_token()  # request override → GITHUB_PAT from .env
    print(f"[publish] REQUEST RECEIVED: repoName={req.repoName!r} visibility={req.visibility!r} "
          f"owner={req.owner!r} project={req.project!r} token={'<provided>' if token else '<none>'}", flush=True)

    # 1) auth — with a PAT, validate the TOKEN itself via an authenticated API call (don't trust
    #    `gh auth status`, which can pass on gh's keyring account while the supplied token is bad);
    #    without a PAT, fall back to checking gh's keyring login.
    if token:
        auth_code, auth_out, auth_err = _gh("api", "user", token=token)
    else:
        auth_code, auth_out, auth_err = _gh("auth", "status")
    print(f"[publish] auth check ({'token' if token else 'gh keyring'}) -> exit={auth_code}", flush=True)
    if auth_code != 0:
        print(f"[publish] REJECTED: auth check failed: {auth_err}", flush=True)
        return {
            "ok": False,
            "outcome": "auth-failed",
            "message": 'Authentication failed — GitHub token expired or missing "repo" scope',
        }

    # 2) owner: request → GITHUB_OWNER (.env) → the token's own login. Validate an explicit owner.
    owner = req.owner.strip() or _env_owner()
    if owner:
        user_ok = _gh("api", f"users/{owner}", token=token)[0] == 0
        org_ok = _gh("api", f"orgs/{owner}", token=token)[0] == 0
        if not (user_ok or org_ok):
            print(f"[publish] REJECTED: invalid owner {owner!r}", flush=True)
            return {
                "ok": False,
                "outcome": "invalid-owner",
                "message": f'Invalid owner "{owner}" — no matching user or organization',
            }
    else:
        code, login, _ = _gh("api", "user", "--jq", ".login", token=token)
        owner = login if code == 0 else ""
        if not owner:
            print("[publish] REJECTED: could not resolve authenticated login", flush=True)
            return {"ok": False, "outcome": "auth-failed", "message": "Could not resolve your GitHub account"}

    # 2b) with a PAT, the token IS the identity — no keyring switch needed (and switching to an
    #     account gh isn't logged into would fail). Only switch active accounts in the no-token path.
    if not token:
        active_login = _gh("api", "user", "--jq", ".login")[1]
        if owner and owner != active_login:
            sw = _gh("auth", "switch", "--hostname", "github.com", "--user", owner)
            print(f"[publish] switch active account {active_login!r} -> {owner!r} exit={sw[0]}", flush=True)

    # 2c) name: an explicit request name is honored as-is; otherwise the agent suggests one from
    #     the built app and we ensure it's free under the owner (so auto-publish never collides).
    explicit = req.repoName.strip()
    if explicit:
        name = explicit
    else:
        name = _unique_repo_name(owner, _suggest_repo_name(req.project), token)
        print(f"[publish] agent-suggested repo name -> {name}", flush=True)

    repo = f"{owner}/{name}"
    print(f"[publish] resolved target repo = {repo}", flush=True)

    # 3) name availability (only an EXPLICIT name can collide — an auto name is already unique)
    if explicit and _gh("repo", "view", repo, token=token)[0] == 0:
        print(f"[publish] REJECTED: {repo} already exists", flush=True)
        return {
            "ok": False,
            "outcome": "repo-exists",
            "message": f'A repository named "{name}" already exists on this account',
        }

    # 4) a generated project must exist on disk to publish
    proj_dir = OUT_DIR / req.project
    has_files = proj_dir.exists() and any(
        p.is_file() and ".git" not in p.parts for p in proj_dir.rglob("*")
    )
    print(f"[publish] proj_dir={proj_dir} exists={proj_dir.exists()} has_files={has_files}", flush=True)
    if not has_files:
        print("[publish] REJECTED: generate-missing", flush=True)
        return {
            "ok": False,
            "outcome": "generate-missing",
            "message": f"No generated project at {proj_dir} — run the code generator first.",
        }

    # 5) REAL: commit (idempotent) + create the GitHub repo + push
    print("[publish] committing locally...", flush=True)
    ex = LocalDiskExecutor(OUT_DIR)
    commit_res = ex.git_commit(req.project, f"IMP-001 publish: {name}")
    print(f"[publish] commit -> committed={commit_res.committed} sha={commit_res.sha}", flush=True)
    print(f"[publish] running gh repo create for {repo} (private={req.visibility != 'public'})...", flush=True)
    res = ex.publish(req.project, repo, private=(req.visibility != "public"), token=token or None)
    print(f"[publish] gh/git result -> exit={res.exit_code}\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}", flush=True)
    if res.exit_code == 0:
        print(f"[publish] SUCCESS -> https://github.com/{repo}", flush=True)
        return {"ok": True, "url": f"https://github.com/{repo}", "repoName": name, "owner": owner}
    print("[publish] FAILED (push-failed)", flush=True)
    return {"ok": False, "outcome": "push-failed", "message": (res.stderr or res.stdout or "Push failed")[:400]}


@app.post("/api/suggest-name")
def suggest_name(req: SuggestNameRequest) -> dict[str, str]:
    """Agent-suggested repository name for the built app, plus the resolved owner (no user input).

    Lets the UI show what the agent picked before it auto-publishes. Uniqueness is resolved
    against the owner so the name shown is the name that will actually be created.
    """
    token = _env_token()
    owner = _env_owner()
    if not owner and token:
        owner = _gh("api", "user", "--jq", ".login", token=token)[1]
    name = _unique_repo_name(owner, _suggest_repo_name(req.project), token)
    print(f"[suggest-name] project={req.project!r} -> {owner or '?'}/{name}", flush=True)
    return {"name": name, "owner": owner}


def _ff_commit_push(project_dir: Path, message: str, push: bool, branch: str) -> tuple[str, str]:
    """git add+commit (skip if no change); optional push. Returns (short_sha, status)."""
    fc._git(["add", "-A"], project_dir)
    if fc._run(["git", "diff", "--cached", "--quiet"], project_dir).returncode == 0:
        return "", "nothing to commit"
    fc._git(["commit", "-m", message], project_dir)
    sha = fc._run(["git", "rev-parse", "--short", "HEAD"], project_dir).stdout.strip()
    if not push:
        return sha, "committed (not pushed)"
    res = fc._run(["git", "push", "-u", "origin", branch], project_dir)
    return sha, ("pushed" if res.returncode == 0 else f"push failed: {(res.stderr or res.stdout).strip()[:200]}")


@app.post("/api/run-feature")
def run_feature(req: RunFeatureRequest) -> dict[str, Any]:
    """Generate ONE user story, commit it as ``feat(US-0X): <title>``, and optionally push.

    The UI calls this once per story (index 0..N-1) to build a per-feature commit history. It is
    CUMULATIVE: prior source files are read back from disk so each story extends the previous ones.
    On ``index == 0`` it (optionally resets and) inits git, ensures the GitHub repo, commits/pushes
    the scaffold to ``main``, then branches ``dev`` from it. Every feature is committed on ``dev``.
    """
    pack = fc._resolve_pack(req.pack)
    stories = fc._parse_stories(pack)
    if not stories:
        raise HTTPException(404, "no user stories (## US-0X — Title) found in user-features.md")
    if req.index < 0 or req.index >= len(stories):
        raise HTTPException(400, f"index {req.index} out of range 0..{len(stories) - 1}")

    project_dir = OUT_DIR / req.project
    base_branch = "main"  # holds ONLY the scaffold
    branch = "dev"        # all feature commits land here; never on main/master
    setup: list[str] = []

    if req.index == 0:
        if req.reset and project_dir.exists():
            fc._force_rmtree(project_dir)
        project_dir.mkdir(parents=True, exist_ok=True)
        fc._git(["init"], project_dir)
        fc._git(["config", "user.email", "codegen@local"], project_dir, check=False)
        fc._git(["config", "user.name", "IMP-001 codegen"], project_dir, check=False)
        if req.push and req.repoName:
            login = req.owner.strip() or _gh("api", "user", "--jq", ".login")[1]
            if not login:
                raise HTTPException(502, "could not resolve a GitHub login (is `gh auth` set up?)")
            slug = f"{login}/{req.repoName}"
            if _gh("repo", "view", slug)[0] != 0:
                vis = "--private" if req.visibility != "public" else "--public"
                code, out, err = _gh("repo", "create", slug, vis)
                if code != 0:
                    raise HTTPException(502, f"repo create failed for {slug}: {err or out}")
            fc._run(["git", "remote", "remove", "origin"], project_dir)
            fc._git(["remote", "add", "origin", f"https://github.com/{slug}.git"], project_dir)
        # scaffold ONLY on main, then branch dev from it
        fc._checkout_branch(project_dir, base_branch)
        for e in render_scaffold(req.project, _load_pack(pack)):
            dest = project_dir / e["path"].lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(e["content"], encoding="utf-8")
        sha0, st0 = _ff_commit_push(project_dir, "chore: initial project scaffold", req.push, base_branch)
        setup.append(f"scaffold(main): {sha0 or '-'} ({st0})")
        fc._ensure_feature_branch(project_dir, branch)  # dev from main

    if not (project_dir / ".git").is_dir():
        raise HTTPException(400, "project not initialized — call with index=0 first")
    fc._ensure_feature_branch(project_dir, branch)  # every feature commits on dev, never main/master

    # Cumulative context: read back the source files produced by earlier stories (frontend +
    # backend) so each feature extends the previous ones.
    current: dict[str, str] = {}
    for p in project_dir.rglob("*"):
        if p.is_file() and ".git" not in p.parts:
            rel = p.relative_to(project_dir).as_posix()
            if (
                rel.startswith(("frontend/", "backend/", "src/"))
                or rel in ("index.html", "vite.config.ts", "vite.config.js")
            ):
                current[rel] = p.read_text(encoding="utf-8")

    sid, title, body = stories[req.index]
    if MODE == "real":
        # Build the feature one layer at a time (Frontend → Backend → Database → Integration →
        # Testing), accumulating files so later layers see earlier ones. Committed as ONE feature.
        files: list[dict[str, str]] = []
        for key, label, instruction in fc._LAYERS:
            ctx = fc._design_context(pack, fc._LAYER_CONTEXT[key])
            layer_files = fc._generate(
                llm_gateway.llm_gateway,
                fc._layer_prompt(ctx, current, sid, title, body, label, instruction),
            )
            for f in layer_files:
                current[f["path"].lstrip("/")] = f["content"]
            files.extend(layer_files)
    else:  # dry-run: canned stub so the flow is demoable with no API key
        files = [{"path": "frontend/src/pages/GamePage.tsx", "content": _stub_content("GamePage.tsx")}]
    for f in files:
        dest = project_dir / f["path"].lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f["content"], encoding="utf-8")
    sha, status = _ff_commit_push(project_dir, f"feat({sid}): {title}", req.push, branch)

    url = ""
    if req.push and req.repoName:
        login = req.owner.strip() or _gh("api", "user", "--jq", ".login")[1]
        url = f"https://github.com/{login}/{req.repoName}"

    done = req.index == len(stories) - 1
    # Keep only at the remote: once the LAST feature is pushed, delete the local working copy.
    if done and req.push and status == "pushed":
        fc._force_rmtree(project_dir)

    return {
        "index": req.index, "total": len(stories), "id": sid, "title": title,
        "files": [f["path"] for f in files], "sha": sha, "status": status,
        "done": done, "url": url, "setup": setup,
    }


@app.get("/api/file", response_class=PlainTextResponse)
def file(run_id: str, path: str) -> str:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    try:
        return run["executor"].read_file(path)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"file not found: {path}") from exc


def _load_pack(pack_dir: Path) -> dict[str, Any]:
    package: dict[str, Any] = {}
    for path in sorted(pack_dir.iterdir()):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            try:
                package[path.name] = json.loads(text)
                continue
            except json.JSONDecodeError:
                pass
        package[path.name] = text
    return package


def main() -> None:
    global MODE, OUT_DIR
    parser = argparse.ArgumentParser(description="IMP-001 code-generator demo server")
    # REAL is the default now: starting the backend uses the real Claude gateway
    # (ANTHROPIC_FOUNDRY_API_KEY from .env). Pass --dry-run for the canned/no-key mode.
    parser.add_argument("--dry-run", action="store_true",
                        help="canned LLM, in-memory, NO API key (opt-in; default is real Claude)")
    parser.add_argument("--real", action="store_true", help="(default) real Claude via Foundry")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR,
                        help=f"where real mode writes generated projects, OUTSIDE the repo (default: {OUT_DIR})")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()

    MODE = "dry-run" if args.dry_run else "real"
    OUT_DIR = args.out_dir.resolve()

    if MODE == "dry-run":
        _install_canned_gateway()
        print(f"IMP-001 demo (DRY-RUN: in-memory, canned LLM, NO API key) -> http://127.0.0.1:{args.port}")
    else:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        print(f"IMP-001 demo (REAL: Claude via ANTHROPIC_FOUNDRY_API_KEY) -> http://127.0.0.1:{args.port}")
        print(f"  generated projects will be written under: {OUT_DIR}")
        print("  (requires ANTHROPIC_FOUNDRY_API_KEY + endpoint in services/implementation/.env)")

    # Show the agents' live progress ([PLANNING]/[GENERATING]/[DONE]) in this terminal.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
