r"""Feature-wise incremental FULL-STACK code generation + commit + push.

For a design pack that lists user stories (``## US-0X — Title`` in ``user-features.md``), generate
the application ONE feature at a time with real Claude — cumulatively, so each step extends the
previous files and keeps earlier features working. Within EACH feature the code is built in this
exact layer order (one LLM pass per layer):

    1. Frontend    — UI, components, forms, client-side validation, API integration
    2. Backend     — routes/controllers, services, business logic
    3. Database    — schema/models, migrations, relationships
    4. Integration — connect frontend, backend, and database
    5. Testing     — tests and verification

After a feature's five layers are done it is committed as ONE conventional commit and pushed:

    feat(US-01): Start a game
    feat(US-02): Place a marker
    ...

Never are two features mixed in one commit. The NEXT feature starts ONLY after the current
feature's commit AND push both succeed — a failed push stops the run (see ``--push``).

Git workflow:
- ``main`` (``--base-branch``, default ``main``) holds ONLY the initial scaffold: the deterministic
  boilerplate is committed as ``chore: initial project scaffold`` and pushed to ``main``.
- ``dev`` (``--branch``, default ``dev``) is branched from ``main`` and is where ALL features are
  implemented. Each feature is committed and pushed to the remote ``dev`` branch independently,
  before the next feature starts.
- Feature commits are refused on ``main``/``master`` — features never land on the scaffold branch.

Storage: by default the code is kept ONLY at the remote — generation happens in a throwaway temp
folder that is DELETED after the run's pushes succeed (nothing persists on disk). Pass
``--out-dir <path>`` to keep a local copy instead (written outside the repo).

The deterministic repo scaffold (Jinja2, no LLM) is rendered once as the first commit.

Usage:
    # keep only at the remote (temp dir, auto-deleted after push) — needs --push + --remote:
    python scripts/feature_commit.py --pack tic-tac-toe --remote https://github.com/you/repo.git --push
    # keep a local copy under <path> instead:
    python scripts/feature_commit.py --pack tic-tac-toe --out-dir C:/Users/Admin/Documents/generated-apps --push

This is a DEV harness (like demo_server.py): it uses the real LLM gateway and shells out to git.
It does NOT go through the LangGraph batch-review workflow — its job is a clean per-feature history.
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_IMPL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_IMPL_DIR))

from app.agents.code_generator import CodeGeneratorAgent  # noqa: E402
from app.services.boilerplate import render_scaffold  # noqa: E402
from app.services.llm_gateway import LLMGateway  # noqa: E402

_REPO_ROOT = _IMPL_DIR.parent if (_IMPL_DIR.parent / "fixtures").is_dir() else _IMPL_DIR.parents[1]
_FIXTURES = _REPO_ROOT / "fixtures"

# Generated projects are written OUTSIDE the SDLC repo so each one's own git repo (on `dev`) never
# nests inside the main tree. Default: a sibling of the repo; override with --out-dir.
_DEFAULT_OUT_DIR = _REPO_ROOT.parent / "generated-apps"

_SYSTEM = """You are an expert FULL-STACK engineer building an application INCREMENTALLY, one \
user story (feature) at a time, and WITHIN each feature strictly in this layer order: \
1) Frontend, 2) Backend, 3) Database, 4) Integration, 5) Testing.

The stack is React 18 + TypeScript (Vite) on the frontend and FastAPI (Python) on the backend, \
with a SQL database (SQLAlchemy models matching the provided schema).

Each turn you implement ONE layer of ONE feature. You receive the design context for that layer, \
the CURRENT source files already on disk, the feature (user story), and the layer to build now.

Return ONLY the files you create or modify for THIS layer. Files you do not return are preserved \
automatically — never resend unchanged files, and never drop or regress earlier features' work.

Rules:
- Real, working, production-quality code. No placeholders, no TODOs, no "// ...".
- Keep the whole app runnable and every previously implemented feature still working.
- Follow the style guide (SKILL.md): naming and structure; NO inline styles (frontend CSS lives \
in src/styles/ derived from the design tokens); copy validation messages VERBATIM from the \
validation rules — never paraphrase.
- Path conventions (project-root-relative):
  - Frontend: frontend/index.html, frontend/src/main.tsx, frontend/src/App.tsx, \
frontend/src/pages/*, frontend/src/components/*, frontend/src/api/*, frontend/src/styles/app.css
  - Backend:  backend/app/main.py, backend/app/routers/*, backend/app/services/*, \
backend/app/schemas/*
  - Database: backend/app/models/*, backend/app/db.py, backend/migrations/*
  - Tests:    backend/tests/*, frontend/src/**/*.test.tsx
- Output STRICT JSON ONLY: {"files":[{"path":"...","content":"..."}],"notes":"..."}. \
No prose, no markdown, no code fences."""

# The five layers, generated in this exact order for every feature. Each is one LLM pass whose
# output is merged into the cumulative workspace before the next layer runs.
_LAYERS: list[tuple[str, str, str]] = [
    (
        "frontend",
        "FRONTEND",
        "Build the UI for this feature: React 18 + TypeScript components, forms, client-side "
        "validation, and the typed API-client calls the UI needs. Files under frontend/src/.",
    ),
    (
        "backend",
        "BACKEND",
        "Implement the backend for this feature: FastAPI routers/controllers, services, and "
        "business logic satisfying the cited endpoints. Files under backend/app/.",
    ),
    (
        "database",
        "DATABASE",
        "Define/extend the database layer for this feature: SQLAlchemy models matching schema.sql, "
        "relationships, and a migration if the schema changed. Files under backend/app/models/ and "
        "backend/migrations/.",
    ),
    (
        "integration",
        "INTEGRATION",
        "Wire the layers together for this feature: connect the frontend API client to the backend "
        "routers, and the backend services to the database models/session (dependency wiring, app "
        "startup, config/env usage). Adjust existing files as needed.",
    ),
    (
        "testing",
        "TESTING",
        "Add tests that verify this feature end-to-end: backend pytest tests for the "
        "endpoints/services and frontend tests for the components. Cover the validation rules "
        "verbatim.",
    ),
]

# Per-layer design-pack context: each layer sees only the artifacts relevant to it, keeping the
# prompt focused. Missing artifacts are simply skipped.
_LAYER_CONTEXT: dict[str, list[tuple[str, str]]] = {
    "frontend": [
        ("SKILL.md", "Conventions / style guide"),
        ("design-tokens.json", "Design tokens"),
        ("validation-rules.md", "Validation rules (COPY MESSAGES VERBATIM)"),
        ("route-list.md", "Routes"),
        ("functional-html-mockup.html", "HTML mockup (reference layout)"),
        ("frontend-project-structure.md", "Frontend project structure"),
    ],
    "backend": [
        ("SKILL.md", "Conventions / style guide"),
        ("api-mapping.csv", "API mapping (endpoints -> handlers)"),
        ("backend-project-structure.md", "Backend project structure"),
        ("backend-structure.json", "Backend structure"),
        ("validation-rules.md", "Validation rules (COPY MESSAGES VERBATIM)"),
    ],
    "database": [
        ("schema.sql", "Database schema (SQL)"),
        ("backend-structure.json", "Backend structure (models)"),
    ],
    "integration": [
        ("route-list.md", "Routes"),
        ("api-mapping.csv", "API mapping (endpoints -> handlers)"),
        ("SKILL.md", "Conventions / style guide"),
    ],
    "testing": [
        ("mandatory-checklist.md", "Mandatory checklist"),
        ("validation-rules.md", "Validation rules (COPY MESSAGES VERBATIM)"),
        ("api-mapping.csv", "API mapping (endpoints -> handlers)"),
    ],
}


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    result = _run(["git", *args], cwd)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result


#: Branches that must never receive FEATURE commits (they hold only the scaffold / merges).
_PROTECTED_BRANCHES = {"main", "master"}


def _checkout_branch(project_dir: Path, branch: str) -> None:
    """Check out ``branch``, creating it from the current HEAD if it doesn't exist yet.

    ``-B`` creates the branch from current HEAD (or as the initial branch on an unborn repo); a
    plain ``checkout`` switches to it when it already exists.
    """
    exists = (
        _run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], project_dir).returncode == 0
    )
    _git(["checkout", branch] if exists else ["checkout", "-B", branch], project_dir)


def _ensure_feature_branch(project_dir: Path, branch: str) -> None:
    """Check out the FEATURE branch (``dev``), refusing ``main``/``master``.

    Feature commits must never land on the scaffold branch — main/master hold only the initial
    scaffold (and later, merges from dev).
    """
    if branch in _PROTECTED_BRANCHES:
        raise SystemExit(f"refusing to put feature commits on protected branch {branch!r}; use 'dev'")
    _checkout_branch(project_dir, branch)


def _force_rmtree(path: Path) -> bool:
    """Recursively delete ``path``, clearing Windows read-only bits (git pack files block a plain
    ``shutil.rmtree`` on Windows). Best-effort; returns True if the tree is gone afterwards."""
    import os
    import stat

    def _clear_readonly(func, p, _exc):  # onexc/onerror handler
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onexc=_clear_readonly)  # Python 3.12+
    except TypeError:  # Python < 3.12 uses onerror
        shutil.rmtree(path, onerror=_clear_readonly)
    except OSError:
        pass
    return not path.exists()


def _resolve_pack(pack: str) -> Path:
    cand = Path(pack)
    for c in ([cand] if cand.is_absolute() else [_FIXTURES / pack, _REPO_ROOT / pack, cand]):
        if c.is_dir() and (c / "user-features.md").exists():
            return c
    raise SystemExit(f"no pack with user-features.md found for {pack!r}")


def _parse_stories(pack: Path) -> list[tuple[str, str, str]]:
    """Return [(id, title, body)] parsed from ``## US-0X — Title`` sections of user-features.md."""
    text = (pack / "user-features.md").read_text(encoding="utf-8")
    parts = re.split(r"\n##\s*(US-\d+)\s*[—:-]\s*(.+)", text)
    stories: list[tuple[str, str, str]] = []
    for i in range(1, len(parts) - 2, 3):
        sid, title, body = parts[i], parts[i + 1].strip(), parts[i + 2]
        body = re.split(r"\n---", body)[0].strip()
        stories.append((sid, title, body))
    return stories


def _design_context(pack: Path, wanted: list[tuple[str, str]]) -> str:
    """Concatenate the requested design-pack artifacts (label + content); skip any that are absent."""
    chunks = []
    for name, label in wanted:
        p = pack / name
        if p.exists():
            chunks.append(f"## {label}\n{p.read_text(encoding='utf-8').strip()}")
    return "\n\n".join(chunks)


def _layer_prompt(
    ctx: str, current: dict[str, str], sid: str, title: str, body: str, label: str, instruction: str
) -> str:
    cur = "\n\n".join(f"### {p}\n```\n{c}\n```" for p, c in current.items()) or "(none yet)"
    return (
        f"Design context for this layer:\n{ctx or '(none provided — build from the feature spec)'}\n\n"
        f"Current source files on disk:\n{cur}\n\n"
        f"Feature (user story):\n{sid} — {title}\n{body}\n\n"
        f"Build this layer now: {label}\n{instruction}\n\n"
        "Return ONLY the files you create or modify for this layer, as strict JSON."
    )


def _generate(gw: LLMGateway, prompt: str) -> list[dict[str, str]]:
    raw = gw.complete(prompt=prompt, system=_SYSTEM)
    files, err = CodeGeneratorAgent._parse(raw)
    if files is None:  # one retry, same pattern as the code_generator agent
        retry = (
            f"{prompt}\n\nYour previous reply was not valid JSON ({err}). Reply with STRICT JSON "
            'only — a single {"files":[{"path":...,"content":...}]} object, nothing else.'
        )
        raw = gw.complete(prompt=retry, system=_SYSTEM)
        files, err = CodeGeneratorAgent._parse(raw)
    if files is None:
        # Log what actually came back so an intermittent bad reply is diagnosable (empty? prose?
        # refusal? truncated?) rather than opaque. Kept short so it doesn't flood the console.
        preview = (raw or "").strip().replace("\n", "\\n")[:300]
        logging.getLogger(__name__).warning(
            "codegen: unparseable reply (%s) len=%d preview=%r", err, len(raw or ""), preview
        )
        raise RuntimeError(f"generation returned invalid JSON: {err} (reply len={len(raw or '')})")
    return files


def _write(project_dir: Path, files: list[dict[str, str]], current: dict[str, str]) -> list[str]:
    written = []
    for f in files:
        rel = f["path"].lstrip("/")
        dest = project_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f["content"], encoding="utf-8")
        current[rel] = f["content"]
        written.append(rel)
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description="Feature-wise incremental full-stack generation + commit + push")
    ap.add_argument("--pack", required=True, help="pack name under fixtures/ or a path")
    ap.add_argument("--project", default="", help="project dir name under --out-dir (default: pack name)")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="keep a LOCAL copy here (outside the repo). Omit to keep the code ONLY at the "
                         "remote (temp dir, deleted after a successful push; requires --push + --remote)")
    ap.add_argument("--push", action="store_true", help="git push after every feature commit (REQUIRED by the workflow; a failed push stops the run before the next feature)")
    ap.add_argument("--remote", default="", help="set 'origin' to this URL before pushing")
    ap.add_argument("--base-branch", default="main", help="scaffold branch (default: main); holds only the scaffold")
    ap.add_argument("--branch", default="dev", help="feature branch (default: dev; main/master refused)")
    ap.add_argument("--fresh", action="store_true", help="wipe the project dir + reinit git first")
    ap.add_argument("--force", action="store_true", help="force-push (use with --fresh on an existing repo)")
    args = ap.parse_args()

    pack = _resolve_pack(args.pack)
    project = args.project or pack.name
    stories = _parse_stories(pack)
    if not stories:
        raise SystemExit("no user stories (## US-0X — Title) found in user-features.md")

    if args.branch in _PROTECTED_BRANCHES:
        raise SystemExit(f"refusing to develop on protected branch {args.branch!r}; use 'dev'")

    # Where the working copy lives. Default (no --out-dir): keep the code ONLY at the remote — work
    # in a throwaway temp dir and delete it after a successful run. Pass --out-dir to persist.
    ephemeral = args.out_dir is None
    if ephemeral:
        if not (args.push and args.remote):
            raise SystemExit(
                "keep-only-at-remote mode (no --out-dir) requires --push and --remote <github url> "
                "so the code is pushed before the local copy is deleted. "
                "Pass --out-dir <path> to keep a local copy instead."
            )
        work_root = Path(tempfile.mkdtemp(prefix="sdlc-gen-"))
    else:
        work_root = args.out_dir.resolve()
    project_dir = work_root / project

    print(
        f"pack={pack}\nproject_dir={project_dir}"
        + ("  (temp - deleted after push; code kept only at remote)" if ephemeral else "")
        + f"\nstories={[s[0] for s in stories]}\nscaffold_branch={args.base_branch}  "
        + f"feature_branch={args.branch}\npush={args.push}\n"
    )
    if not args.push:
        print("WARNING: --push not set. The required workflow pushes each feature to the remote "
              "before starting the next; commits will be LOCAL ONLY this run.\n")

    if args.fresh and project_dir.exists():
        _force_rmtree(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)

    if not (project_dir / ".git").is_dir():
        _git(["init"], project_dir)
    if args.remote:
        _run(["git", "remote", "remove", "origin"], project_dir)  # ignore failure
        _git(["remote", "add", "origin", args.remote], project_dir)
    _git(["config", "user.email", "codegen@local"], project_dir, check=False)
    _git(["config", "user.name", "IMP-001 codegen"], project_dir, check=False)

    def commit_and_push(message: str, branch: str) -> None:
        _git(["add", "-A"], project_dir)
        if _run(["git", "diff", "--cached", "--quiet"], project_dir).returncode == 0:
            print("   (nothing to commit)")
            return
        _git(["commit", "-m", message], project_dir)
        print(f"   committed [{branch}]: {message}")
        if not args.push:
            print("   (push skipped: pass --push to push to the remote)")
            return
        push = ["push", "-u", "origin", branch] + (["--force"] if args.force else [])
        res = _run(["git", *push], project_dir)
        if res.returncode != 0:
            # Workflow rule: start the next feature ONLY after commit AND push succeed.
            raise SystemExit(
                f"   push FAILED for {message!r} on {branch} — stopping before the next feature.\n"
                f"   {(res.stderr or res.stdout).strip()[:500]}"
            )
        print(f"   push -> origin/{branch} OK")

    try:
        # main: SCAFFOLD ONLY (deterministic Jinja2, no LLM, no features), committed + pushed to main.
        _checkout_branch(project_dir, args.base_branch)
        design_package = {p.name: p.read_text(encoding="utf-8") for p in pack.iterdir() if p.is_file()}
        for entry in render_scaffold(project, design_package):
            dest = project_dir / entry["path"].lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(entry["content"], encoding="utf-8")
        print(f"[scaffold] rendered boilerplate on '{args.base_branch}'")
        commit_and_push("chore: initial project scaffold", args.base_branch)

        # dev: branched from main; ALL features land here (never on main).
        _ensure_feature_branch(project_dir, args.branch)
        print(f"[branch] '{args.branch}' created from '{args.base_branch}' — features commit here\n")

        # 1..N) one FEATURE per commit; within each feature, the five layers in order, cumulative.
        gw = LLMGateway()
        current: dict[str, str] = {}
        for sid, title, body in stories:
            print(f"=== {sid} — {title} ===")
            for key, label, instruction in _LAYERS:
                ctx = _design_context(pack, _LAYER_CONTEXT[key])
                print(f"  [{label}] generating with Claude ...")
                files = _generate(gw, _layer_prompt(ctx, current, sid, title, body, label, instruction))
                written = _write(project_dir, files, current)
                print(f"     files: {written or '(none)'}")
            # One commit for the whole feature (all five layers), then push before the next feature.
            commit_and_push(f"feat({sid}): {title}", args.branch)
    except BaseException:
        # On ANY failure (incl. a push failure that stops the run) keep the temp copy so nothing is
        # lost — the code may not be fully at the remote yet.
        if ephemeral:
            print(f"\n[run did not finish — local copy KEPT at {project_dir} (not deleted)]")
        raise

    done_msg = (
        f"scaffold pushed to '{args.base_branch}', {len(stories)} feature(s) pushed to '{args.branch}'"
    )
    if ephemeral:
        deleted = _force_rmtree(work_root)
        if deleted:
            print(f"\nDONE. {done_msg}. Local copy deleted - code is kept only at the remote.")
        else:
            print(f"\nDONE. {done_msg}. "
                  f"WARNING: could not fully delete the local temp copy at {work_root}")
    else:
        print(f"\nDONE. {done_msg}. Local copy at {project_dir}")


if __name__ == "__main__":
    main()
