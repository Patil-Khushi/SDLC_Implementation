"""Run ONLY the Code Review agent against a public GitHub repo (no full pipeline).

Clones the repo into the ephemeral review sandbox (Docker), runs ruff/eslint — and sonar-scanner
if SonarQube is enabled in .env — then asks the LLM for a review and writes the Markdown report
to reports/<project>-<run>.md.

Usage (from services/implementation/, venv active):
    python scripts/run_review.py <github_repo_url> [--project NAME] [--skill path/to/SKILL.md]

Requires: Docker running + the sdlc-review-sandbox image built, and ANTHROPIC_API_KEY in .env.
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path

# Make `app...` importable when run as `python scripts/run_review.py` from the service root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _setup_logging() -> None:
    """Show the agent's step-by-step progress logs on the console."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-5s  %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "urllib3", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

from app.agents.code_review import CodeReviewAgent  # noqa: E402
from app.graph.state import new_state  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Code Review agent on a public GitHub repo.")
    parser.add_argument("repo_url", help="Public GitHub repository URL to review")
    parser.add_argument("--project", default="review", help="Project id (used in the report filename)")
    parser.add_argument("--skill", default=None, help="Optional path to a SKILL.md style guide")
    args = parser.parse_args()
    _setup_logging()

    design_package: dict[str, str] = {}
    if args.skill:
        design_package["SKILL.md"] = Path(args.skill).read_text(encoding="utf-8")

    state = new_state(
        run_id=uuid.uuid4().hex[:8],
        attempt=0,
        project_id=args.project,
        design_package=design_package,
    )
    state["repo_url"] = args.repo_url

    print(f"Reviewing {args.repo_url} (sandbox clone -> lint/scan -> report) ...\n")
    out = CodeReviewAgent().execute(state)

    report_path = out.get("review_report_path", "")
    findings_path = out.get("review_findings_path", "")
    folder = str(Path(report_path).parent) if report_path else "(none)"

    # Clear, prominent "saved" block at the very end of the run.
    print("\n" + "=" * 72)
    print("  CODE REVIEW COMPLETE - SAVED")
    print("=" * 72)
    print(f"  Folder    : {folder}")
    print(f"  Report    : {report_path}")
    print(f"  Findings  : {findings_path}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
