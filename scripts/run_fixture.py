"""Run the IMP-001 code-generation subgraph against a design-pack directory.

The HTTP API (`POST /implementation/start`) does NOT build `work_items` for you yet — it only
takes `design_package` (see CLAUDE.md's open gaps). This script does the missing step itself:
it decomposes the pack with `plan_builder.build_plan()`, then drives the compiled graph directly.

Usage (from services/implementation/, with .venv active):

    # dry run: FakeExecutor + a canned LLM reply, no Docker, no API key — proves the wiring
    python scripts/run_fixture.py ../../fixtures/ecommerce_complete --dry-run

    # REAL build to a standalone product repo: real Claude (needs Foundry creds in .env) writes
    # real files to <out-dir>/<project> and makes a real local git commit there. No Docker
    # (completeness-only gate). --yes auto-approves the HITL so the initial commit is made.
    python scripts/run_fixture.py ../../fixtures/ecommerce_complete --real --yes \
        --project ecommerce --out-dir generated

    # real run inside the exec-sandbox (needs SANDBOX_MCP_URL reachable)
    python scripts/run_fixture.py ../../fixtures/ecommerce_complete --sandbox-url http://localhost:8080/mcp

If a run pauses at batch_review (workflow_status == "pending_review") and you did NOT pass --yes,
resume it via the API `POST /implementation/{run_id}/review {"approved": true}` or by re-running
with --yes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so `app.*` imports work

from langgraph.types import Command  # noqa: E402

from app.graph.graph import workflow  # noqa: E402
from app.graph.state import new_state  # noqa: E402
from app.integrations.executor import Executor, FakeExecutor, MCPExecutor, set_executor  # noqa: E402
from app.services.plan_builder import build_plan  # noqa: E402
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


def _interactive_publish(executor: LocalDiskExecutor, project: str) -> None:
    """Prompt for repo name / visibility / owner, then create the GitHub repo and push.

    Runs only when the generation completed and was approved (i.e. there IS a local commit).
    Needs an authenticated `gh` CLI. Must run in a real terminal (it reads stdin).
    """
    print("\n=== Create GitHub repository (agent publish step) ===")
    name = ""
    while not name:
        name = input("Repository name: ").strip()
    vis = input("Visibility [private/public] (default private): ").strip().lower()
    private = vis != "public"
    owner = input("Owner (leave blank to use your authenticated account): ").strip()
    if not owner:
        owner = executor.run_command(["gh", "api", "user", "--jq", ".login"]).stdout.strip()
    repo = f"{owner}/{name}"

    print(f"\nCreating {'PRIVATE' if private else 'PUBLIC'} repo {repo} and pushing…")
    res = executor.publish(project, repo, private=private)
    if res.stdout:
        print(res.stdout)
    if res.stderr:
        print(res.stderr)
    if res.exit_code == 0:
        print(f"\n[OK] Pushed. Repo: https://github.com/{repo}")
    else:
        print(f"\n[FAILED] publish exited {res.exit_code} - check output above (repo may exist, or gh auth).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack_dir", type=Path, help="design-pack directory, e.g. fixtures/ecommerce_complete")
    parser.add_argument("--dry-run", action="store_true", help="FakeExecutor + canned LLM reply (no Docker/API key)")
    parser.add_argument("--real", action="store_true",
                        help="REAL Claude + LocalDiskExecutor: build a standalone product repo on disk")
    parser.add_argument("--yes", action="store_true",
                        help="auto-approve the HITL batch review (make the commit without prompting)")
    parser.add_argument("--publish", action="store_true",
                        help="--real: after a completed run, interactively create a GitHub repo "
                             "(prompts for name / visibility / owner via gh) and push")
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
        help="--real: base dir for the product repo (<out-dir>/<project>); --dry-run: dump in-memory files here",
    )
    args = parser.parse_args()

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

    executor: Executor
    if args.dry_run:
        from app.services import llm_gateway

        llm_gateway.llm_gateway.complete = _canned_llm_reply  # type: ignore[method-assign]
        llm_gateway.llm_gateway.complete_with_tools = lambda prompt, **kw: _canned_llm_reply(prompt)  # type: ignore[method-assign]
        executor = FakeExecutor()
    elif args.real:
        out_base = (args.out_dir or Path("generated")).resolve()
        executor = LocalDiskExecutor(out_base)
        print(f"REAL build with Claude -> product repo at {out_base / args.project}")
    else:
        executor = asyncio.run(MCPExecutor.connect(args.sandbox_url))
    set_executor(executor)

    run_id = args.project
    initial = new_state(
        run_id=run_id, attempt=0, project_id=args.project,
        design_package=design_package, work_items=work_items,
    )
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 1000}
    workflow.invoke(initial, config)
    state = workflow.get_state(config).values

    # HITL: auto-approve when --yes, so the batch commit is made in one shot.
    if state.get("workflow_status") == "pending_review" and args.yes:
        print("\n[HITL] auto-approving (--yes) -> committing the whole run")
        workflow.invoke(Command(resume={"approved": True, "rejections": {}}), config)
        state = workflow.get_state(config).values

    print("\n--- generation_summary ---")
    print(state.get("generation_summary", "(empty)"))
    print("--- workflow_status:", state.get("workflow_status"), "---")
    print(f"generated_code: {len(state.get('generated_code', []))} file(s)")

    if args.dry_run and args.out_dir is not None and isinstance(executor, FakeExecutor):
        out_dir = args.out_dir.resolve()
        for path, content in executor.files.items():
            dest = out_dir / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        print(f"\nWrote {len(executor.files)} in-memory file(s) to {out_dir}")

    if args.real and isinstance(executor, LocalDiskExecutor):
        repo = executor.root / args.project
        print(f"\nProduct repo: {repo}")
        print(f"  inspect with:  git -C \"{repo}\" log --oneline  &&  git -C \"{repo}\" ls-files")

    # Agent publish step: create the GitHub repo + push, AFTER generation completed and was
    # approved (a local commit exists). Interactive — prompts for name / visibility / owner.
    if args.real and args.publish and isinstance(executor, LocalDiskExecutor):
        if state.get("workflow_status") == "completed":
            _interactive_publish(executor, args.project)
        else:
            print(f"\n[publish skipped] run status is {state.get('workflow_status')!r}, not 'completed' "
                  "(need generation done + approved before creating the repo).")

    if state.get("workflow_status") == "pending_review":
        print(f"\nPaused for batch review (no --yes). Resume with thread_id/run_id = {run_id!r}:")
        print(f'  POST /implementation/{run_id}/review   {{"approved": true}}')


if __name__ == "__main__":
    main()
