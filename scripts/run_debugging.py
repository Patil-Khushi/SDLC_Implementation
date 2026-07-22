"""Run ONLY the Debugging phase (compile/build check + LLM fix loop) against an EXISTING
generated repo — no design pack, no regeneration.

Unlike ``run_fixture.py`` (which builds from a design pack all the way through Debugging),
this is for the case where you already have generated code on GitHub (from an earlier full
run) and just want to compile/build-check + auto-fix it. Clones the repo, runs
``debug_check_node`` (compile + build), and on failure calls ``DebuggingAgent`` to propose a
fix, re-checking up to ``DEBUG_CAP`` times — mirroring the graph's debug/debug_check loop
exactly (same nodes, same cap), just without the design-pack/codegen steps before it.

Usage (from services/implementation/, venv active):
    python scripts/run_debugging.py <github_repo_url> [--project NAME] [--branch dev] \\
        [--commit <sha>] [--workspace DIR] [--push --remote <owner/name>] [--token <PAT>]

Requires: git on PATH; the project's own toolchain (Node/Python/etc., whatever `compile`/
`build` shell out to) on PATH; ANTHROPIC_FOUNDRY_* creds in .env for the LLM fix loop.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import uuid
from pathlib import Path

# Make `app...` importable when run as `python scripts/run_debugging.py` from the service root.
_IMPL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_IMPL_DIR))
sys.path.insert(0, str(_IMPL_DIR / "scripts"))


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-5s  %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "urllib3", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


from app.agents.debugging import DebuggingAgent  # noqa: E402
from app.graph.nodes import debug_check_node  # noqa: E402
from app.graph.state import new_state  # noqa: E402
from app.integrations.executor import set_executor  # noqa: E402
from local_executor import LocalDiskExecutor  # noqa: E402

DEBUG_CAP = 3  # same cap as app/graph/router.py's debug loop


def _list_generated_files(executor: LocalDiskExecutor, project_id: str) -> list[str]:
    """List every tracked file in the clone as a workspace-relative (project-prefixed) path, so
    the Debugging agent can read them for context on a failure (mirrors ``generated_code``)."""
    result = executor.run_command(["git", "ls-files"], cwd=project_id)
    if result.exit_code != 0:
        return []
    return [f"{project_id}/{line}" for line in result.stdout.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Debugging phase (compile/build + LLM fix loop) against an existing repo."
    )
    parser.add_argument("repo_url", help="Public GitHub repository URL to clone and debug")
    parser.add_argument("--project", default=None, help="Project id (default: derived from repo_url)")
    parser.add_argument("--run-id", default=None, help="Run id for reports/logs (default: random)")
    parser.add_argument("--branch", default="dev", help="Branch to check out and (if --push) push to")
    parser.add_argument("--commit", default=None, help="Exact commit to check out (overrides --branch tip)")
    parser.add_argument("--workspace", default=None, help="Local root to clone into (default: workspace/)")
    parser.add_argument("--push", action="store_true", help="Commit + push fixes to --branch")
    parser.add_argument("--remote", default=None, help="owner/name to push to (required with --push)")
    parser.add_argument("--token", default=None, help="GitHub PAT for the push (else uses `gh auth`)")
    args = parser.parse_args()
    _setup_logging()

    if args.push and not args.remote:
        parser.error("--push requires --remote <owner/name>")

    project_id = args.project or re.sub(r"[^A-Za-z0-9._-]", "-", args.repo_url.rstrip("/").rsplit("/", 1)[-1])
    run_id = args.run_id or uuid.uuid4().hex[:8]
    workspace = Path(args.workspace) if args.workspace else _IMPL_DIR / "workspace" / f"{project_id}-{run_id}"

    executor = LocalDiskExecutor(workspace)
    set_executor(executor)

    print(f"Cloning {args.repo_url} -> {workspace / project_id} ...")
    executor.run_command(["git", "clone", args.repo_url, "."], cwd=project_id, timeout=300)
    ref = args.commit or args.branch
    checkout = executor.run_command(["git", "checkout", ref], cwd=project_id)
    if checkout.exit_code != 0:
        print(f"ERROR: could not check out {ref!r}: {checkout.stderr}", file=sys.stderr)
        return 1

    state = new_state(run_id=run_id, attempt=0, project_id=project_id,
                       push_enabled=args.push, git_remote=args.remote or "", git_token=args.token or "")
    state["branch"] = args.branch
    state["generated_code"] = _list_generated_files(executor, project_id)
    print(f"Tracked {len(state['generated_code'])} file(s) in the clone for fix-loop context.\n")

    debugging_agent = DebuggingAgent(executor=executor)
    for attempt in range(1, DEBUG_CAP + 1):
        print(f"--- debug_check attempt {attempt}/{DEBUG_CAP} ---")
        state = debug_check_node(state)
        result = state["debug_result"]
        for check in result["checks"]:
            mark = "PASS" if check["passed"] else "FAIL"
            print(f"  [{mark}] {check['name']}" + (f": {check['stderr'][:300]}" if not check["passed"] else ""))
        if result["passed"]:
            state["workflow_status"] = "debugged"
            break
        if attempt == DEBUG_CAP:
            state["workflow_status"] = "needs_human_review"
            break
        print(f"\nDebug check failed — invoking DebuggingAgent to propose a fix (attempt {attempt}) ...\n")
        state = debugging_agent.execute(state)

    print("\n" + "=" * 72)
    print("  DEBUGGING COMPLETE")
    print("=" * 72)
    print(f"  Passed     : {state['debug_result']['passed']}")
    print(f"  Attempts   : {state.get('debug_attempt', 0)}")
    print(f"  Status     : {state.get('workflow_status')}")
    print("=" * 72)

    if state["debug_result"]["passed"] and state.get("debug_attempt", 0) > 0:
        message = f"debug({run_id}): fix compile/build failures"
        if args.push and hasattr(executor, "publish_feature"):
            print(f"\nPublishing (commit + push to {args.branch}) ...")
            pub = executor.publish_feature(project_id, message, state["generated_code"],
                                            feature_branch=args.branch, token=args.token or None)
            print("pushed" if pub.exit_code == 0 else f"PUSH FAILED: {pub.stderr}")
        else:
            commit = executor.git_commit(project_id, message)
            print("committed locally" if commit.committed else f"COMMIT FAILED: {commit.stderr}")
    elif state.get("debug_attempt", 0) == 0:
        print("\nNo fixes were needed — compile/build passed on the first check.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
