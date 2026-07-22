"""Run the IMP-001 code-generation subgraph against a design-pack directory.

The HTTP API (`POST /implementation/start`) does NOT build `work_items` for you yet — it only
takes `design_package` (see CLAUDE.md's open gaps). This script does the missing step itself:
it decomposes the pack with `plan_builder.build_plan()`, then drives the compiled graph directly.

Usage (from services/implementation/, with .venv active):

    # DEFAULT = the whole real flow in one command: real Claude builds the code, creates a PUBLIC
    # GitHub repo and pushes it DURING the run (main early, features live), and every agent runs
    # inline (Code Review clones the repo, Refactoring edits it, Debugging, Unit Testing). Needs
    # Foundry creds in .env + an authenticated `gh` (repo = $GITHUB_OWNER/<--project>).
    python scripts/run_fixture.py ../fixtures/authentication --only login --project auth-live-demo -y

    # opt-outs:
    #   --no-publish   build + commit locally, do NOT create/push a GitHub repo
    #   --private      create a private repo (inline Code Review can't clone it, so it no-ops)
    #   --dry-run      FakeExecutor + canned LLM (no Docker/API key/push) - wiring test only
    #   --sandbox      run inside the MCP exec-sandbox instead of the local-disk build

Human-in-the-loop was removed: a completed plan auto-commits (workflow_status == "completed"); a
repair-cap failure ends flagged "needs_human_review" (no pause, no resume).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so `app.*` imports work

from app.graph.graph import workflow  # noqa: E402
from app.graph.state import new_state  # noqa: E402
from app.integrations.executor import Executor, FakeExecutor, MCPExecutor, set_executor  # noqa: E402
from app.services.plan_builder import build_plan  # noqa: E402
from scripts.feature_commit import _DEFAULT_OUT_DIR  # noqa: E402  (generated output goes outside the repo)
from scripts.local_executor import LocalDiskExecutor  # noqa: E402


def _load_pack(pack_dir: Path) -> dict[str, Any]:
    """Load a pack's top-level artifacts into a name -> content dict (.json parsed, else text)."""
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


def _canned_llm_reply(prompt: str, **_kw: Any) -> str:
    """Dry-run stand-in: return placeholder content for every file the prompt actually asks for."""
    match = re.search(r"Target files \(produce ONLY these\):\n((?:- .+\n?)*)", prompt)
    lines = match.group(1).splitlines() if match else []
    paths = [ln[2:].strip() for ln in lines if ln.startswith("- ")]
    paths = [p for p in paths if p and p != "(none specified)"]
    files = [{"path": p, "content": f"# placeholder for {p}\n"} for p in paths] or [
        {"path": "placeholder.py", "content": "# placeholder\n"}
    ]
    return json.dumps({"files": files, "notes": "dry-run canned content"})



def _confirm_plan(work_items: list, *, auto_yes: bool) -> bool:
    """Print the build plan in the terminal and ask for approval BEFORE code generation.

    Returns True to proceed. With ``auto_yes`` (``--yes``) it prints the plan and proceeds without
    prompting (non-interactive / CI). CLI-only gate — the automated service / HTTP API still runs
    with no human-in-the-loop.
    """
    print("\n" + "=" * 70)
    print(f"BUILD PLAN - {len(work_items)} work item(s) the Code Generator will produce:")
    print("=" * 70)
    for i, w in enumerate(work_items, 1):
        bits = []
        if getattr(w, "endpoints", None):
            bits.append("endpoints=" + ", ".join(w.endpoints))
        if getattr(w, "tables", None):
            bits.append("tables=" + ", ".join(w.tables))
        if getattr(w, "screens", None):
            bits.append("screens=" + ", ".join(w.screens))
        meta = ("  [" + " | ".join(bits) + "]") if bits else ""
        print(f"\n{i}. {w.id}{meta}")
        for path in w.target_files:
            print(f"     - {path}")
    print("\n" + "=" * 70)
    if auto_yes:
        print("Auto-approved (--yes) - proceeding.\n")
        return True
    try:
        answer = input("Proceed with code generation for this plan? [y/N]: ").strip().lower()
    except EOFError:  # non-interactive stdin (piped/CI) without --yes -> don't proceed silently
        answer = ""
    return answer in ("y", "yes")


def _short(val: Any, limit: int = 100) -> str:
    """One-line, truncated repr of a shared-state value for the terminal dump."""
    if isinstance(val, str):
        one = " ".join(val.split())
        return f'"{one}"' if len(one) <= limit else f'"{one[:limit]}..." ({len(val)} chars)'
    if isinstance(val, list):
        head = "; ".join(_short(v, 40) for v in val[:3])
        return f"[{len(val)} item(s)]" + (f" {head}{'; ...' if len(val) > 3 else ''}" if val else "")
    if isinstance(val, dict):
        keys = ", ".join(list(val)[:6])
        return f"{{{len(val)} key(s): {keys}{'...' if len(val) > 6 else ''}}}"
    return repr(val)


def _dump_state(state: dict[str, Any]) -> None:
    """Print the shared WorkflowState — the ONE object every agent reads & writes. It's defined in
    app/graph/state.py, threaded through every graph node, and read here via workflow.get_state()."""
    print("\n" + "=" * 70)
    print("SHARED STATE  (WorkflowState - every agent reads & writes this one object)")
    print("  defined in: app/graph/state.py   |   read via: workflow.get_state(config).values")
    print("=" * 70)
    for key in sorted(state):
        print(f"  {key:22} = {_short(state[key])}")
    print("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack_dir", type=Path, help="design-pack directory, e.g. fixtures/ecommerce_complete")
    # Real is the DEFAULT: one command runs the whole real flow (all agents) and publishes to a
    # PUBLIC GitHub repo DURING the run so Code Review clones + reviews it inline. Opt out below.
    parser.add_argument("--dry-run", action="store_true",
                        help="FakeExecutor + canned LLM (no Docker/API key, no push) - wiring test only")
    parser.add_argument("--sandbox", action="store_true",
                        help="run inside the MCP exec-sandbox instead of the local-disk build (needs a server)")
    parser.add_argument("--real", action="store_true", help="(default) REAL Claude + LocalDiskExecutor")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="skip the terminal plan-approval prompt (auto-approve the plan)")
    parser.add_argument("--no-publish", action="store_true",
                        help="real mode: do NOT create/push a GitHub repo (local commits only; inline "
                             "Code Review then has no repo to clone)")
    parser.add_argument("--private", action="store_true",
                        help="real mode: create a PRIVATE repo (default: PUBLIC). NOTE inline Code "
                             "Review clones in a container and needs a PUBLIC repo to analyze.")
    parser.add_argument("--publish", action="store_true", help=argparse.SUPPRESS)  # back-compat no-op (on by default)
    parser.add_argument("--public", action="store_true", help=argparse.SUPPRESS)   # back-compat no-op (on by default)
    parser.add_argument("--owner", default=None,
                        help="GitHub owner for the published repo (default: $GITHUB_OWNER, then the gh account)")
    parser.add_argument("--repo-name", default=None, help="repo name to create (default: --project)")
    parser.add_argument("--sandbox-url", default="http://localhost:8080/mcp")
    parser.add_argument("--project", "--project-id", dest="project", default="fixture-run",
                        help="project name — also the repo subfolder under --out-dir in --real mode")
    parser.add_argument(
        "--only", default=None,
        help="only generate work items whose id contains this substring, e.g. --only login "
             "(matches backend-loginUser + frontend-login). Cheap way to test one feature.",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=None,
        help=f"--real: base dir for the product repo (<out-dir>/<project>), OUTSIDE the repo "
             f"(default: {_DEFAULT_OUT_DIR}); --dry-run: dump in-memory files here",
    )
    args = parser.parse_args()

    # Real is the DEFAULT; --dry-run / --sandbox are explicit opt-outs. In real mode we publish to a
    # PUBLIC repo by default so the whole flow (incl. inline Code Review) runs in one command — opt
    # out with --no-publish / --private. (--publish / --public stay accepted as explicit no-ops.)
    mode = "dry-run" if args.dry_run else "sandbox" if args.sandbox else "real"
    do_publish = mode == "real" and not args.no_publish
    make_public = not args.private

    # Show the agents' live progress ([PLANNING]/[GENERATING]/[DONE]) in this terminal.
    # This script drives the graph directly (it never imports app.main), so nothing has
    # configured the root logger yet — without this, Python suppresses INFO lines.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    pack_dir = args.pack_dir.resolve()
    design_package = _load_pack(pack_dir)
    work_items = build_plan(pack_dir)
    if args.only:
        needle = args.only.lower()
        work_items = [w for w in work_items if needle in w.id.lower()]
        if not work_items:
            all_ids = ", ".join(w.id for w in build_plan(pack_dir))
            print(f"No work items match --only {args.only!r}.\nAvailable ids: {all_ids}")
            return
    print(
        f"Built {len(work_items)} work item(s)"
        + (f" matching --only '{args.only}'" if args.only else "")
        + f" from {pack_dir}: "
        + ", ".join(w.id for w in work_items)
    )

    # Terminal plan-approval gate (CLI-only — the automated service / HTTP API still has no HITL).
    if not _confirm_plan(work_items, auto_yes=args.yes):
        print("\nAborted - no code generated (plan not approved).")
        return

    executor: Executor
    push_enabled = False
    git_remote = ""
    git_token = ""
    if mode == "dry-run":
        from app.services import llm_gateway

        llm_gateway.llm_gateway.complete = _canned_llm_reply  # type: ignore[method-assign]
        llm_gateway.llm_gateway.complete_with_tools = lambda prompt, **kw: _canned_llm_reply(prompt)  # type: ignore[method-assign]
        executor = FakeExecutor()
    elif mode == "sandbox":
        executor = asyncio.run(MCPExecutor.connect(args.sandbox_url))
    else:  # real (default)
        out_base = (args.out_dir or _DEFAULT_OUT_DIR).resolve()
        executor = LocalDiskExecutor(out_base, private=not make_public)
        print(f"REAL build with Claude -> product repo at {out_base / args.project}")
        if do_publish:
            # Push DURING the run: commit_node creates the repo via gh, pushes, and sets repo_url,
            # so Code Review clones + reviews it INLINE (one command, every agent real).
            owner = (args.owner or os.environ.get("GITHUB_OWNER", "")).strip()
            if not owner:
                owner = executor.run_command(["gh", "api", "user", "--jq", ".login"]).stdout.strip()
            git_remote = f"{owner}/{args.repo_name or args.project}"
            git_token = os.environ.get("GITHUB_PAT", "").strip()
            push_enabled = True
            vis = "PUBLIC" if make_public else "PRIVATE"
            print(f"  pushing to {vis} github.com/{git_remote} during the run -> Code Review reviews it inline")
            if not make_public:
                print("  NOTE: inline Code Review clones in a container and needs a PUBLIC repo; "
                      "drop --private if the review can't clone a private repo.")
        else:
            print("  --no-publish: local commits only (no GitHub repo; inline Code Review will be a no-op)")
    set_executor(executor)

    run_id = args.project
    initial = new_state(
        run_id=run_id, attempt=0, project_id=args.project,
        design_package=design_package, work_items=work_items,
        push_enabled=push_enabled, git_remote=git_remote, git_token=git_token,
    )
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 1000}
    workflow.invoke(initial, config)
    state = workflow.get_state(config).values  # runs to completion (auto-commit, no HITL)

    print("\n--- generation_summary ---")
    print(state.get("generation_summary", "(empty)"))
    print("--- workflow_status:", state.get("workflow_status"), "---")
    print(f"generated_code: {len(state.get('generated_code', []))} file(s)")

    _dump_state(state)  # show the shared WorkflowState (what every agent read & wrote)

    if args.dry_run and args.out_dir is not None and isinstance(executor, FakeExecutor):
        out_dir = args.out_dir.resolve()
        for path, content in executor.files.items():
            dest = out_dir / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        print(f"\nWrote {len(executor.files)} in-memory file(s) to {out_dir}")

    if args.real and isinstance(executor, LocalDiskExecutor):
        repo = executor.root / args.project
        print(f"\nProduct repo (local): {repo}")
        print(f"  inspect with:  git -C \"{repo}\" log --oneline  &&  git -C \"{repo}\" ls-files")

    # --publish pushed the repo DURING the run (see above), so there is no separate publish step —
    # report where it landed and what Code Review did with it inline.
    if push_enabled:
        status = state.get("workflow_status")
        if status in ("push_failed", "commit_failed"):
            print(f"\n[publish] push FAILED during the run (status={status}) — see generation_summary above.")
        else:
            url = state.get("repo_url") or f"https://github.com/{git_remote}"
            print(f"\n[publish] pushed to {url}")
            print(f"[review]  Code Review ran inline — report: {state.get('review_report_path') or '(none)'}")


if __name__ == "__main__":
    main()
