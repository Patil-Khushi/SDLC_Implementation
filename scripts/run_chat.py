"""Drive conversational turns against an ALREADY-GENERATED project — no full pipeline run.

A real pipeline run takes 3-6 hours, so nobody can iterate on the conversational loop by starting
one. This harness points the same nodes at a project that already exists on disk and runs a turn in
seconds: queue a message, plan a patch, apply it to the plan, and optionally carry the resulting
``action="modify"`` items through the generator and the gate so the edit really lands.

    python scripts/run_chat.py generated-apps/live-proof-auth --say "add a logout endpoint"
    python scripts/run_chat.py <dir> --say "drop the wishlist" --say "make login use OTP"
    python scripts/run_chat.py <dir> --say "..." --dry-run       # canned replies, no model calls
    python scripts/run_chat.py <dir> --say "..." --apply         # also make the edits

The plan diff printed for each turn IS the demo: it shows a request becoming a bounded change to
the pending work, rather than a rebuild.

A planner REFUSAL is a normal, successful outcome — vague, cross-cutting and rename requests are
declined by design — so it is reported plainly and exits 0.

Requires: ANTHROPIC_FOUNDRY_* creds in .env unless --dry-run.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

# Make `app...` importable when run as `python scripts/run_chat.py` from the service root.
_IMPL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_IMPL_DIR))
sys.path.insert(0, str(_IMPL_DIR / "scripts"))


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING,
                        format="%(asctime)s  %(levelname)-5s  %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "urllib3", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


from app.graph import nodes  # noqa: E402
from app.graph.state import WorkflowState, new_state  # noqa: E402
from app.integrations.executor import set_executor  # noqa: E402
from app.models import WorkItem  # noqa: E402
from app.services import chat_inbox, requirement_ledger  # noqa: E402
from app.services import llm_gateway  # noqa: E402
from local_executor import LocalDiskExecutor  # noqa: E402

#: Directories that are never part of a generated app's own source.
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".pytest_cache"}


def _canned_for(first_item_id: str) -> Any:
    """Build a --dry-run planner reply that actually patches something.

    A dry-run that always refused would exercise the plumbing while demonstrating nothing, so this
    proposes one ``modify`` against a real work item: the plan diff, the ledger-based file
    resolution and (with ``--apply``) the gate all run for real. Only the model is absent.

    With ``--apply`` the edit itself is a no-op, because nothing drives the editing tools — and the
    gate's ``files_changed`` check is then *supposed* to fail. That is the honest dry-run outcome
    and a useful thing to see.
    """
    def _reply(prompt: str, **_: Any) -> str:
        return json.dumps({
            "ops": [{
                "op": "modify",
                "target_work_item_ids": [first_item_id],
                "intent": "dry-run: pretend the user asked to change this work item",
            }],
            "notes": "dry-run: no model was called",
            "refusal": "",
        })
    return _reply


def _scan_project(project_dir: Path) -> list[str]:
    """Project-relative source paths that exist on disk right now."""
    out: list[str] = []
    for path in sorted(project_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(project_dir)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        out.append(rel.as_posix())
    return out


def _seed_plan(plan_path: Path | None, project_dir: Path) -> tuple[list[WorkItem], list[dict[str, Any]]]:
    """Build a plan + ledger to patch.

    With ``--plan`` the real work-items JSON is used. Without it, one synthetic work item per
    top-level source directory is derived from disk — enough to demonstrate resolution, and honest
    about being a stand-in (a real run's plan and ledger come from the run itself).
    """
    files = _scan_project(project_dir)
    if plan_path is not None:
        raw = json.loads(plan_path.read_text(encoding="utf-8"))
        items = [WorkItem(**d) for d in (raw.get("items") if isinstance(raw, dict) else raw)]
    else:
        groups: dict[str, list[str]] = {}
        for rel in files:
            groups.setdefault(rel.split("/")[0] or "root", []).append(rel)
        items = [
            WorkItem(id=f"WI-{i:03d}", feature_id=name, feature_title=name,
                     target_files=paths[:5])
            for i, (name, paths) in enumerate(sorted(groups.items()), start=1)
        ]

    # Everything on disk counts as already generated — which is the situation this harness models.
    ledger: list[dict[str, Any]] = []
    for item in items:
        present = [p for p in item.target_files if p in set(files)]
        if present:
            ledger = requirement_ledger.record(ledger, item, present)
    return items, ledger


def _print_plan(label: str, items: list[WorkItem], ledger: list[dict[str, Any]]) -> None:
    print(f"\n  {label} ({len(items)} item(s))")
    for item in items:
        mark = "generated" if requirement_ledger.is_generated(ledger, item.id) else "pending  "
        print(f"    {mark}  {item.id:<14} [{item.action}] {item.feature_title or '-'}")


def _print_turn(report: dict[str, Any], diff: dict[str, Any]) -> None:
    if report.get("refusal"):
        print(f"\n  REFUSED: {report['refusal']}")
        print("  (a refusal is a successful outcome - ask again more specifically)")
    for key, verb in (("added", "added"), ("removed", "removed"), ("modified", "modified")):
        for wid in report.get(key) or []:
            print(f"  {verb:<9} {wid}")
    for line in report.get("skipped") or []:
        print(f"  skipped   {line}")
    for line in report.get("errors") or []:
        print(f"  ERROR     {line}")
    if report.get("orphans"):
        print(f"  orphaned  {', '.join(report['orphans'])}")
    print(f"  plan      {diff.get('count_before')} -> {diff.get('count_after')}")


def _apply_modify_items(state: WorkflowState, cursor_before: int) -> None:
    """Carry every new modify item through code_generator + the gate, so the edit really lands."""
    from app.agents.code_generator import CodeGeneratorAgent

    items: list[WorkItem] = state.get("work_items") or []
    for idx in range(cursor_before, len(items)):
        if items[idx].action not in ("modify", "delete"):
            continue
        state["work_item_index"] = idx
        state = nodes.select_work_item_node(state)
        item = state["current_work_item"]
        print(f"\n  editing {item.id} -> {', '.join(item.target_files)}")
        state = CodeGeneratorAgent().execute(state)
        state = nodes.gate_node(state)
        gate = state.get("gate_result") or {}
        for check in gate.get("checks") or []:
            flag = "ok  " if check["passed"] else "FAIL"
            print(f"    {flag} {check['name']}"
                  + (f" - {check['stderr'][:160]}" if not check["passed"] else ""))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Drive chat-driven plan patches against an already-generated project.",
    )
    parser.add_argument("project_dir", type=Path, help="an already-generated app directory")
    parser.add_argument("--say", action="append", default=[], required=True,
                        metavar="TEXT", help="a change request; repeat for several turns")
    parser.add_argument("--project", default=None,
                        help="project/thread id (default: the directory name)")
    parser.add_argument("--plan", type=Path, default=None,
                        help="work-items JSON to seed the plan (default: derive it from disk)")
    parser.add_argument("--dry-run", action="store_true",
                        help="canned planner replies; makes no model calls")
    parser.add_argument("--apply", action="store_true",
                        help="also run code_generator + the gate over the resulting modify items")
    parser.add_argument("--show-plan", action="store_true", help="print the plan around each turn")
    parser.add_argument("-v", "--verbose", action="store_true", help="agent/node logging")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    project_dir = args.project_dir.resolve()
    if not project_dir.is_dir():
        print(f"error: {project_dir} is not a directory", file=sys.stderr)
        return 2
    project = args.project or project_dir.name

    # The workspace is the PARENT: node code addresses files as <project_id>/<rel>, so project_id
    # must be the directory name and the executor rooted one level above it.
    set_executor(LocalDiskExecutor(project_dir.parent))

    items, ledger = _seed_plan(args.plan, project_dir)
    if not items:
        print(f"error: no source files found under {project_dir}", file=sys.stderr)
        return 2
    if args.dry_run:
        llm_gateway.llm_gateway.complete = _canned_for(items[0].id)  # type: ignore[method-assign]
    state = new_state(run_id=project, attempt=0, project_id=project_dir.name, chat_enabled=True)
    state["work_items"] = items
    state["requirement_ledger"] = ledger
    # Everything seeded is already built, so the whole plan is behind the cursor and only NEW items
    # are pending — which is exactly the state a conversation joins mid-run.
    state["work_item_index"] = len(items)

    print(f"project: {project_dir}")
    print(f"inbox:   {chat_inbox.inbox_path(project)}")
    if args.show_plan:
        _print_plan("plan before", items, ledger)

    for turn, text in enumerate(args.say, start=1):
        seq = chat_inbox.append(project, text, received_at="1970-01-01T00:00:00Z")
        print(f"\n--- turn {turn}: {text!r} (seq {seq}) ---")

        cursor_before = int(state.get("work_item_index", 0))
        state = nodes.chat_intake_node(state)

        log = state.get("chat_log") or []
        if not log or log[-1].get("seq") != seq:
            print("  (no turn was applied - is the message already consumed?)")
            continue
        _print_turn(log[-1].get("report") or {}, log[-1].get("diff") or {})

        if args.apply:
            _apply_modify_items(state, cursor_before)

        if args.show_plan:
            _print_plan(f"plan after turn {turn}",
                        state.get("work_items") or [], state.get("requirement_ledger") or [])

    if state.get("orphan_files"):
        print(f"\norphaned files (unwired, NOT deleted): {', '.join(state['orphan_files'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
