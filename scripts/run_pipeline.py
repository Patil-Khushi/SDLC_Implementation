"""Interactive terminal runner — type your input, then watch ALL 5 agents run in sequence.

This is the "observe the whole pipeline from the terminal" entry point. It prompts you (from the
terminal) for a design pack and a run mode, then drives the full compiled LangGraph workflow,
printing a banner as EACH node/agent executes so you can see the automatic hand-off order live:

    Code Generator -> (commit) -> Code Reviewer -> Refactoring -> Debugging -> Unit Testing

Every agent's own INFO logs stream inline (real-time), and a "== stage ==" marker prints after
each node so the sequence is easy to follow. Same graph the HTTP API uses (app.graph.graph) — no
behaviour is faked except the LLM/executor in --dry-run.

Usage (from SDLC_Implementation/, with .venv active):

    # fully interactive: prompts for pack + mode
    ./.venv/Scripts/python.exe scripts/run_pipeline.py

    # dry run (no API key, no Docker): FakeExecutor + canned LLM — proves the wiring end-to-end
    ./.venv/Scripts/python.exe scripts/run_pipeline.py --pack ../fixtures/authentication --dry-run

    # REAL build with Claude (needs creds in .env) writing files to disk + a real local git commit
    ./.venv/Scripts/python.exe scripts/run_pipeline.py --pack ../fixtures/authentication --real

The Code Generator's INPUT is a design pack (a folder the plan builder decomposes into work
items), NOT a free-text prompt — the whole pipeline is design-pack driven. Use --only to run a
single feature (cheaper).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so `app.*` imports work

from app.graph.graph import workflow  # noqa: E402
from app.graph.state import new_state  # noqa: E402
from app.integrations.executor import Executor, FakeExecutor, MCPExecutor, set_executor  # noqa: E402
from app.services.plan_builder import build_plan  # noqa: E402
from scripts.local_executor import LocalDiskExecutor  # noqa: E402
from scripts.run_fixture import (  # noqa: E402 (reuse, no duplication)
    _canned_llm_reply, _confirm_plan, _dump_state, _load_pack, _short,
)

#: Friendly label per graph node so the terminal shows which of the 5 agents is running. The five
#: numbered ones ARE the agents in the order the graph runs them; the rest are fixed infra steps.
STAGE_LABELS = {
    "scaffold": "SCAFFOLD  (Jinja2 boilerplate, no LLM)",
    "select": "select work item",
    "code_generator": "AGENT 1/5 - CODE GENERATOR",
    "gate": "gate (files_complete)",
    "repair": "repair (LLM fixes missing files)",
    "commit": "COMMIT  (fixed git step)",
    "code_review": "AGENT 2/5 - CODE REVIEWER",
    "refactoring": "AGENT 3/5 - REFACTORING",
    "debug_check": "debug check (compile + build)",
    "debugging": "AGENT 4/5 - DEBUGGING",
    "unit_test_generate": "AGENT 5/5 - UNIT TESTING (generate)",
    "unit_test_run": "AGENT 5/5 - UNIT TESTING (run)",
    "escalate": "ESCALATE  (needs_human_review)",
}


def _pick(prompt: str, options: list[str]) -> str:
    """Small terminal menu — print numbered options, read a choice from stdin."""
    for i, opt in enumerate(options, 1):
        print(f"  {i}) {opt}")
    while True:
        raw = input(f"{prompt} [1-{len(options)}]: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print("  (please enter one of the numbers)")


def _resolve_pack(args_pack: str | None) -> Path:
    """Return the chosen design-pack dir — from --pack, or an interactive menu of ../fixtures/*."""
    if args_pack:
        return Path(args_pack).resolve()
    fixtures = Path(__file__).resolve().parents[2] / "fixtures"
    packs = sorted(p for p in fixtures.iterdir() if p.is_dir()) if fixtures.is_dir() else []
    if not packs:
        print(f"No design packs found under {fixtures}. Pass one with --pack <dir>.")
        raise SystemExit(1)
    print(f"\nDesign packs available under {fixtures}:")
    chosen = _pick("Pick a design pack", [p.name for p in packs])
    return (fixtures / chosen).resolve()


def _stage_marker(node: str, delta: dict) -> None:
    """Print a marker after a node runs, listing the shared-state fields IT wrote — so you watch
    the WorkflowState object evolve agent by agent (this is the shared state, changing live)."""
    label = STAGE_LABELS.get(node, node)
    print(f"\n{'=' * 70}\n== DONE: {label}\n{'=' * 70}", flush=True)
    if delta:
        print("   shared-state fields written by this step:")
        for key in sorted(delta):
            print(f"     {key} = {_short(delta[key])}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pack", default=None, help="design-pack dir (skips the interactive menu)")
    parser.add_argument("--dry-run", action="store_true", help="FakeExecutor + canned LLM (no API key/Docker)")
    parser.add_argument("--real", action="store_true", help="real Claude + LocalDiskExecutor (writes files to disk)")
    parser.add_argument("--sandbox-url", default="http://localhost:8080/mcp", help="--sandbox mode MCP url")
    parser.add_argument("--sandbox", action="store_true", help="real Claude inside the exec-sandbox (MCP)")
    parser.add_argument("--only", default=None, help="only run work items whose id contains this substring")
    parser.add_argument("--project", default="pipeline-run", help="project id / run id")
    parser.add_argument("--out-dir", type=Path, default=None, help="--real: base dir for the product repo")
    parser.add_argument("--yes", "-y", action="store_true", help="skip the plan-approval prompt (auto-approve)")
    args = parser.parse_args()

    # Stream every agent's INFO logs to this terminal in real time (this script drives the graph
    # directly and never imports app.main, so nothing else has configured the root logger).
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    pack_dir = _resolve_pack(args.pack)
    if not pack_dir.is_dir():
        print(f"Design pack not found: {pack_dir}")
        raise SystemExit(1)

    # Mode: from a flag, or ask.
    mode = "dry-run" if args.dry_run else "real" if args.real else "sandbox" if args.sandbox else None
    if mode is None:
        print("\nRun mode:")
        mode = _pick(
            "Pick a mode",
            ["dry-run (no API key, no Docker - proves the wiring)",
             "real (real Claude, writes files to disk)",
             "sandbox (real Claude in the MCP exec-sandbox - needs a server at --sandbox-url)"],
        ).split()[0]

    design_package = _load_pack(pack_dir)
    work_items = build_plan(pack_dir)
    if args.only:
        needle = args.only.lower()
        work_items = [w for w in work_items if needle in w.id.lower()]
    if not work_items:
        print(f"No work items built from {pack_dir}" + (f" matching --only {args.only!r}" if args.only else ""))
        raise SystemExit(1)

    print(f"\nInput pack: {pack_dir}")
    print(f"Work items ({len(work_items)}): " + ", ".join(w.id for w in work_items))
    print(f"Mode: {mode}")

    # Terminal plan-approval gate before any agent runs.
    if not _confirm_plan(work_items, auto_yes=args.yes):
        print("\nAborted - no code generated (plan not approved).")
        raise SystemExit(0)

    executor: Executor
    if mode == "dry-run":
        from app.services import llm_gateway
        llm_gateway.llm_gateway.complete = _canned_llm_reply  # type: ignore[method-assign]
        llm_gateway.llm_gateway.complete_with_tools = lambda prompt, **kw: _canned_llm_reply(prompt)  # type: ignore[method-assign]
        executor = FakeExecutor()
    elif mode == "real":
        out_base = (args.out_dir or Path("generated")).resolve()
        executor = LocalDiskExecutor(out_base)
        print(f"REAL build with Claude -> product repo at {out_base / args.project}\n")
    else:
        # Sandbox mode needs a running MCP exec-sandbox server at --sandbox-url. If it isn't up the
        # connect throws a deep httpx.ConnectError inside a TaskGroup; catch it and explain rather
        # than dumping a 100-line traceback the user can't act on.
        try:
            executor = asyncio.run(MCPExecutor.connect(args.sandbox_url))
        except BaseException as exc:  # noqa: BLE001 - anyio wraps the real error in an ExceptionGroup
            print(
                f"\n[sandbox] Could not connect to the exec-sandbox at {args.sandbox_url}\n"
                f"          ({type(exc).__name__}: {exc})\n\n"
                "Sandbox mode requires a running MCP sandbox server at that URL. If you don't have\n"
                "one up, use a mode that needs no external service instead:\n"
                "  --dry-run   FakeExecutor + canned LLM (no API key, no Docker) - proves the wiring\n"
                "  --real      real Claude, writes files + a local git commit to ./generated/\n"
                "Or start the sandbox and pass its URL with --sandbox-url http://host:port/mcp\n"
            )
            raise SystemExit(1) from None
    set_executor(executor)

    initial = new_state(
        run_id=args.project, attempt=0, project_id=args.project,
        design_package=design_package, work_items=work_items,
    )
    config = {"configurable": {"thread_id": args.project}, "recursion_limit": 1000}

    # stream(updates) yields one {node: state_delta} per node as it finishes — this is how we show
    # the agents running one after another. All the agents' own logs still stream live above each.
    print("Starting pipeline — agents run automatically, one after the other:\n")
    for chunk in workflow.stream(initial, config, stream_mode="updates"):
        for node, delta in chunk.items():
            _stage_marker(node, delta or {})

    state = workflow.get_state(config).values
    print("\n" + "#" * 70)
    print("# PIPELINE FINISHED")
    print("#" * 70)
    print(f"workflow_status : {state.get('workflow_status')}")
    print(f"generated_code  : {len(state.get('generated_code', []))} file(s)")
    print(f"unit_tests      : {len(state.get('unit_tests', []))} test file(s)")
    print(f"review_report   : {state.get('review_report_path') or '(none)'}")
    print(f"refactored_code : {state.get('refactored_code') or '(none)'}")
    print("\n--- generation_summary ---")
    print(state.get("generation_summary", "(empty)"))

    _dump_state(state)  # full shared WorkflowState at the end (per-step deltas printed live above)


if __name__ == "__main__":
    main()
