"""Run ONLY the Refactoring agent (+ its publish step) against an EXISTING code-review report.

Unlike ``run_fixture.py`` (which builds a design pack from scratch and drives the whole graph),
this is for the case where Code Review already ran elsewhere and you have its findings.json —
this script clones the reviewed repo locally, points Refactoring at those findings, and
(optionally) commits + pushes the fixes to the working branch, exactly like the fixed
``refactoring_publish`` graph node does.

Findings from the review sandbox record ``file`` as an ABSOLUTE clone-mount path
(``/work/repo/<rel>``) for some tools and an already-repo-relative path for others (mixed by
tool). Both shapes are normalized here to the plain repo-relative path Refactoring expects
(``--strip-prefix``, default ``/work/repo/``, is stripped only when present).

Usage (from services/implementation/, venv active):
    python scripts/run_refactoring.py <github_repo_url> --findings <path/to/findings.json> \\
        [--project NAME] [--branch dev] [--commit <sha>] [--workspace DIR] \\
        [--push --remote <owner/name>] [--token <PAT>]

Requires: git on PATH; ANTHROPIC_FOUNDRY_* creds in .env for the agentic edit loop; `gh` CLI
(or --token) if --push is used.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import uuid
from pathlib import Path
from typing import Any

# Make `app...` importable when run as `python scripts/run_refactoring.py` from the service root.
_IMPL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_IMPL_DIR))
sys.path.insert(0, str(_IMPL_DIR / "scripts"))


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-5s  %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "urllib3", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


from app.agents.refactoring import RefactoringAgent  # noqa: E402
from app.graph.nodes import refactoring_publish_node  # noqa: E402
from app.graph.state import new_state  # noqa: E402
from app.integrations.executor import set_executor  # noqa: E402
from local_executor import LocalDiskExecutor  # noqa: E402


def _normalize_findings(raw_path: Path, strip_prefix: str) -> list[dict[str, Any]]:
    """Strip the review-sandbox clone-mount prefix from each finding's ``file`` (only when
    present — SonarQube findings are already repo-relative, so they pass through unchanged)."""
    findings = json.loads(raw_path.read_text(encoding="utf-8"))
    if not isinstance(findings, list):
        raise ValueError(f"{raw_path}: expected a JSON list of findings")
    out = []
    for f in findings:
        if isinstance(f, dict) and isinstance(f.get("file"), str) and f["file"].startswith(strip_prefix):
            f = {**f, "file": f["file"][len(strip_prefix):]}
        out.append(f)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Refactoring agent (+ publish) against an existing findings.json."
    )
    parser.add_argument("repo_url", help="Public GitHub repository URL to clone and fix")
    parser.add_argument("--findings", required=True, help="Path to the review's findings.json")
    parser.add_argument("--project", default=None, help="Project id (default: derived from repo_url)")
    parser.add_argument("--run-id", default=None, help="Run id for the reports/ folder (default: random)")
    parser.add_argument("--branch", default="dev", help="Branch to check out and (if --push) push to")
    parser.add_argument("--commit", default=None, help="Exact commit to check out (overrides --branch tip)")
    parser.add_argument("--workspace", default=None, help="Local root to clone into (default: a temp dir)")
    parser.add_argument("--strip-prefix", default="/work/repo/",
                        help="Sandbox clone-mount prefix to strip from finding file paths")
    parser.add_argument("--push", action="store_true", help="Commit + push the fixes to --branch")
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

    normalized = _normalize_findings(Path(args.findings), args.strip_prefix)
    findings_path = workspace / "findings.normalized.json"
    findings_path.write_text(json.dumps(normalized, indent=2), encoding="utf-8")
    print(f"Normalized {len(normalized)} finding(s) -> {findings_path}")

    state = new_state(run_id=run_id, attempt=0, project_id=project_id,
                       push_enabled=args.push, git_remote=args.remote or "", git_token=args.token or "")
    state["review_findings_path"] = str(findings_path)
    state["branch"] = args.branch

    print("\nRunning the Refactoring agent (agentic read_file/write_file edit loop) ...\n")
    state = RefactoringAgent(executor=executor).execute(state)

    print("\n" + "=" * 72)
    print("  REFACTORING COMPLETE")
    print("=" * 72)
    print(f"  Summary   : {state.get('refactored_code')}")
    print(f"  Files     : {state.get('refactored_files') or '(none edited)'}")
    print(f"  Report    : {state.get('refactoring_report_path', '(not saved)')}")
    print(f"  Status    : {state.get('workflow_status')}")
    print("=" * 72)

    if state.get("refactored_files"):
        print(f"\nPublishing (commit{' + push to ' + args.branch if args.push else ' locally'}) ...")
        state = refactoring_publish_node(state)
        print(state.get("generation_summary", "").strip())
    else:
        print("\nNothing was edited — skipping publish.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
