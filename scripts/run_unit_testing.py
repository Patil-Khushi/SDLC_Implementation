"""Run ONLY the Unit Testing phase against an EXISTING generated repo — no design pack.

Mirrors the graph's post-Debugging loop: ``unit_test_generate`` (LLM writes tests) ->
``unit_test_run`` (``executor.test()``) -> on failure, ``DebuggingAgent`` proposes a fix (the
SAME agent/counter the compile/build loop uses — see ``app/agents/debugging.py``'s
``_current_failure``) -> re-run, up to ``DEBUG_CAP`` attempts total (shared with the compile/
build loop's cap, per ``app/graph/router.py``).

Since there's no design pack here, ``work_items`` (which the Unit Test agent iterates to know
what to test) don't exist — this script builds SYNTHETIC ones by grouping the repo's own source
files by directory (one work item per module directory), so every module gets a test file.

Usage (from services/implementation/, venv active):
    python scripts/run_unit_testing.py <github_repo_url> [--project NAME] [--branch dev] \\
        [--commit <sha>] [--workspace DIR] [--push --remote <owner/name>] [--token <PAT>]

Requires: git on PATH; the project's own test runner (whatever `executor.test()` shells out to)
on PATH; ANTHROPIC_FOUNDRY_* creds in .env.
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
import uuid
from pathlib import Path

# Make `app...` importable when run as `python scripts/run_unit_testing.py` from the service root.
_IMPL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_IMPL_DIR))
sys.path.insert(0, str(_IMPL_DIR / "scripts"))


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-5s  %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "urllib3", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


from app.agents.debugging import DebuggingAgent  # noqa: E402
from app.agents.unit_test import UnitTestAgent  # noqa: E402
from app.graph.state import new_state  # noqa: E402
from app.integrations.executor import set_executor  # noqa: E402
from app.models import WorkItem  # noqa: E402
from local_executor import LocalDiskExecutor  # noqa: E402

DEBUG_CAP = 3  # shared cap for the compile/build + test fix loop (app/graph/router.py)

# Files that don't need a unit test of their own (boilerplate/config/data, not app logic).
_SKIP_BASENAMES = {
    "Dockerfile", ".gitignore", "README.md", "package.json", "package-lock.json",
    ".env.example", ".eslintrc.js", "jest.config.js", "docker-compose.yml", "knexfile.js",
}
_SKIP_DIR_PARTS = {"migrations", "seeds", "node_modules", "tests", "test", "__tests__"}
_SOURCE_EXTENSIONS = {".js", ".ts", ".py"}


def _npm(name: str) -> str:
    """Resolve npm/npx to their real executable (npm.cmd on Windows — plain subprocess
    without shell=True can't launch the extensionless shim)."""
    return shutil.which(name) or name


def _run_real_tests(executor: LocalDiskExecutor, project_id: str) -> dict:
    """Run the repo's ACTUAL test suite (npm install + jest). ``LocalDiskExecutor.test()`` is a
    completeness-gate stub that always passes — useless here, where the whole point is to see
    whether the generated tests really pass against the generated code."""
    install = executor.run_command([_npm("npm"), "install", "--no-audit", "--no-fund"],
                                    cwd=project_id, timeout=600)
    if install.exit_code != 0:
        return {"name": "test", "passed": False, "exit_code": install.exit_code,
                "stderr": f"npm install failed:\n{(install.stderr or install.stdout)[-2000:]}"}
    run = executor.run_command([_npm("npx"), "jest", "--ci", "--silent"], cwd=project_id, timeout=900)
    stderr = (run.stderr or run.stdout)[-3000:]
    return {"name": "test", "passed": run.exit_code == 0, "exit_code": run.exit_code,
            "stderr": "" if run.exit_code == 0 else stderr}


def _build_synthetic_work_items(executor: LocalDiskExecutor, project_id: str) -> list[WorkItem]:
    """Group the repo's own source files by directory into one WorkItem per module, so
    UnitTestAgent (which iterates ``work_items`` to know what to test) has something to work
    from despite no design-pack decomposition existing for this run."""
    result = executor.run_command(["git", "ls-files"], cwd=project_id)
    if result.exit_code != 0:
        return []

    by_dir: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        path = line.strip()
        if not path or Path(path).name in _SKIP_BASENAMES:
            continue
        if Path(path).suffix not in _SOURCE_EXTENSIONS:
            continue
        parts = Path(path).parts
        if _SKIP_DIR_PARTS & set(parts):
            continue
        by_dir.setdefault(str(Path(path).parent), []).append(path)

    items = []
    for i, (dir_path, files) in enumerate(sorted(by_dir.items())):
        item_id = re.sub(r"[^A-Za-z0-9._-]", "-", dir_path) or f"module-{i}"
        items.append(WorkItem(
            id=item_id, requirement_ids=[], endpoints=[], tables=[], screens=[],
            target_files=sorted(files),
        ))
    return items


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Unit Testing phase (generate + run tests + fix loop) against an existing repo."
    )
    parser.add_argument("repo_url", help="Public GitHub repository URL to clone and test")
    parser.add_argument("--project", default=None, help="Project id (default: derived from repo_url)")
    parser.add_argument("--run-id", default=None, help="Run id for reports/logs (default: random)")
    parser.add_argument("--branch", default="dev", help="Branch to check out and (if --push) push to")
    parser.add_argument("--commit", default=None, help="Exact commit to check out (overrides --branch tip)")
    parser.add_argument("--workspace", default=None, help="Local root to clone into (default: workspace/)")
    parser.add_argument("--push", action="store_true", help="Commit + push the tests/fixes to --branch")
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

    result = executor.run_command(["git", "ls-files"], cwd=project_id)
    state["generated_code"] = [f"{project_id}/{p}" for p in result.stdout.splitlines() if p.strip()]

    work_items = _build_synthetic_work_items(executor, project_id)
    state["work_items"] = work_items
    print(f"Grouped {sum(len(wi.target_files) for wi in work_items)} source file(s) into "
          f"{len(work_items)} synthetic work item(s) (one per module directory).\n")

    print("Generating unit tests (UnitTestAgent, one LLM call per module) ...\n")
    state = UnitTestAgent(executor=executor).execute(state)
    print(f"Wrote {len(state.get('unit_tests', []))} test file(s); tests_ok={state.get('tests_ok')}\n")

    debugging_agent = DebuggingAgent(executor=executor)
    for attempt in range(1, DEBUG_CAP + 1):
        print(f"--- unit_test_run attempt {attempt}/{DEBUG_CAP} (npm install + jest) ---")
        try:
            check = _run_real_tests(executor, project_id)
        except Exception as exc:  # noqa: BLE001 - mirrors unit_test_run_node's own handling
            check = {"name": "test", "passed": False, "stderr": f"executor error: {exc}", "exit_code": -1}
        state["test_result"] = {"passed": check["passed"], "checks": [check]}
        mark = "PASS" if check["passed"] else "FAIL"
        print(f"  [{mark}] {check['name']}" + (f": {check['stderr'][:300]}" if not check["passed"] else ""))
        if check["passed"]:
            state["workflow_status"] = "completed"
            break
        if attempt == DEBUG_CAP or int(state.get("debug_attempt", 0)) >= DEBUG_CAP:
            state["workflow_status"] = "needs_human_review"
            break
        print(f"\nTest run failed — invoking DebuggingAgent to propose a fix (attempt {attempt}) ...\n")
        state = debugging_agent.execute(state)

    print("\n" + "=" * 72)
    print("  UNIT TESTING COMPLETE")
    print("=" * 72)
    print(f"  Tests written : {len(state.get('unit_tests', []))}")
    print(f"  Test passed   : {state['test_result']['passed']}")
    print(f"  Fix attempts  : {state.get('debug_attempt', 0)}")
    print(f"  Status        : {state.get('workflow_status')}")
    print("=" * 72)

    produced = list(state.get("unit_tests", []))
    if produced or state.get("debug_attempt", 0) > 0:
        message = f"test({run_id}): add generated unit tests" + (
            " + debug fixes" if state.get("debug_attempt", 0) > 0 else ""
        )
        paths = produced + [p for p in state.get("generated_code", []) if p not in produced]
        if args.push and hasattr(executor, "publish_feature"):
            print(f"\nPublishing (commit + push to {args.branch}) ...")
            pub = executor.publish_feature(project_id, message, paths,
                                            feature_branch=args.branch, token=args.token or None)
            print("pushed" if pub.exit_code == 0 else f"PUSH FAILED: {pub.stderr}")
        else:
            commit = executor.git_commit(project_id, message)
            print("committed locally" if commit.committed else f"COMMIT FAILED: {commit.stderr}")
    else:
        print("\nNothing produced (no tests written, no fixes applied) — skipping publish.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
