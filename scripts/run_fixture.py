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
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so `app.*` imports work

from app.graph.graph import resolve_checkpoint_db_path, workflow  # noqa: E402
from app.graph.state import new_state  # noqa: E402
from app.integrations.executor import Executor, FakeExecutor, MCPExecutor, set_executor  # noqa: E402
from app.services.plan_builder import build_plan  # noqa: E402
from app.services.run_transcript import (  # noqa: E402
    DEFAULT_REPLAY_SECONDS,
    RunTranscript,
    artifacts_from_state,
)
from scripts.feature_commit import _DEFAULT_OUT_DIR  # noqa: E402  (generated output goes outside the repo)
from scripts.local_executor import LocalDiskExecutor  # noqa: E402

#: Where each run's replayable transcript lands (one subfolder per --project). Inside the service
#: dir, not next to the product repo: it is a recording of OUR pipeline, not part of the app built.
_DEFAULT_TRANSCRIPT_DIR = Path(__file__).resolve().parent.parent / "run-transcripts"


def _resolve_owner(owner_arg: str | None) -> str:
    """The GitHub owner a published repo would be created under: --owner, $GITHUB_OWNER, then `gh`."""
    owner = (owner_arg or os.environ.get("GITHUB_OWNER", "")).strip()
    if owner:
        return owner
    try:
        result = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                                capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _name_is_taken(name: str, *, out_base: Path, owner: str, check_github: bool) -> bool:
    """True if anything from a previous run already answers to ``name``.

    All three homes a project name occupies are checked, because reusing ANY of them corrupts the
    next run differently: the local folder (a new scaffold writes into last run's working tree),
    the GitHub repo (features push onto last run's branches), and the LangGraph checkpoint (stale
    state fields silently skip whole phases — the case ``main``'s guard already aborts on).
    """
    if (out_base / name).exists():
        return True
    if workflow.get_state({"configurable": {"thread_id": name}}).values:
        return True
    if check_github and owner:
        try:
            result = subprocess.run(["gh", "repo", "view", f"{owner}/{name}"],
                                    capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return False  # can't ask GitHub — don't invent a collision
        return result.returncode == 0
    return False


def _fresh_project_name(project: str, *, out_base: Path, owner: str, check_github: bool) -> str:
    """``project`` if it is genuinely unused, else the same name with a timestamp suffix.

    Opt-in (``--fresh``). Without it the run still aborts on a reused name and asks the operator to
    choose — this only automates that choice, for the case where re-running the same pack under a
    predictable name is the point (a nightly build, a demo recording).
    """
    if not _name_is_taken(project, out_base=out_base, owner=owner, check_github=check_github):
        return project
    stamp = datetime.now().strftime("%m%d-%H%M%S")
    candidate = f"{project}-{stamp}"
    suffix = 1
    while _name_is_taken(candidate, out_base=out_base, owner=owner, check_github=check_github):
        candidate = f"{project}-{stamp}-{suffix}"
        suffix += 1
    print(f"--fresh: {project!r} is already in use (local folder, GitHub repo and/or checkpoint) — "
          f"this run uses {candidate!r} instead.")
    return candidate


def _load_pack(pack_dir: Path) -> dict[str, Any]:
    """Load a pack's top-level artifacts into a name -> content dict (.json parsed, else text)."""
    package: dict[str, Any] = {}
    for path in sorted(pack_dir.iterdir()):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError):
            # Binary artifacts (e.g. .docx/.pdf) carry no text role the resolver uses — skip them.
            continue
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


#: Substrings that mark a shared-state field as credential-shaped — its value is NEVER printed.
#: `git_token` (a raw GitHub PAT, set from $GITHUB_PAT) is the one such field today; the substring
#: match also covers any future token/secret/password/key field so a credential can't leak via the
#: state dump (into stdout, CI logs, a pasted bug report, ...).
_SECRET_KEY_MARKERS = ("token", "secret", "password", "passwd", "pat", "api_key", "apikey", "credential")


def _is_secret_key(key: str) -> bool:
    k = key.lower()
    return any(marker in k for marker in _SECRET_KEY_MARKERS)


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
        value = "<redacted>" if _is_secret_key(key) else _short(state[key])
        print(f"  {key:22} = {value}")
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
        "--fresh", action="store_true",
        help="if --project is already in use (local folder, GitHub repo or checkpoint), append a "
             "timestamp and run under THAT name instead of aborting - so a repeatable command "
             "(nightly build, demo recording) never writes into or pushes onto a previous run",
    )
    parser.add_argument(
        "--only", default=None,
        help="only generate work items whose id contains this substring, e.g. --only login "
             "(matches backend-loginUser + frontend-login). Cheap way to test one feature.",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="resume a run that crashed mid-graph, from its last completed node (via the "
             "graph's SQLite checkpointer, keyed by --project as thread_id) — no re-read of the "
             "pack, no rebuilt work_items, no redone LLM calls. Only works for a crash that "
             "happened with this checkpointer in place; a run whose checkpoint predates it (or "
             "used --dry-run/FakeExecutor, whose in-memory files don't survive a restart) has "
             "nothing to resume from — start a fresh run under a new --project name instead.",
    )
    parser.add_argument(
        "--no-record", action="store_true",
        help="do NOT record this run's output for replay (recording is on by default; a recorded "
             "run can be replayed in ~2 min with scripts/demo_replay.py instead of re-run in hours)",
    )
    parser.add_argument(
        "--transcript-dir", type=Path, default=None,
        help=f"where to write this run's replayable transcript (default: {_DEFAULT_TRANSCRIPT_DIR})",
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

    if args.no_record:
        _configure_logging()
        _run(args, mode=mode, do_publish=do_publish, make_public=make_public)
        return

    # Recording is ON by default: a run takes hours, so "we forgot the flag" is unrecoverable
    # without repeating them. The tee must be installed BEFORE logging is configured — a
    # StreamHandler binds sys.stderr by VALUE, so configuring first would leave every agent
    # progress line (the bulk of what a viewer watches) out of the recording.
    transcript_dir = (args.transcript_dir or _DEFAULT_TRANSCRIPT_DIR).resolve()
    state: dict[str, Any] | None = None
    with RunTranscript(transcript_dir, project=args.project) as transcript:
        _configure_logging()
        try:
            state = _run(args, mode=mode, do_publish=do_publish, make_public=make_public)
        finally:
            # In a finally: a crashed or Ctrl-C'd run still leaves a replayable transcript with a
            # manifest describing how far it got.
            transcript.finish(artifacts_from_state(
                state or {}, extra={"design_pack": str(args.pack_dir), "mode": mode},
            ))
    print(f"\n[record] transcript: {transcript.dir}")
    print(f"[record] replay it in ~{int(DEFAULT_REPLAY_SECONDS)}s with:  "
          f"python scripts/demo_replay.py --project {args.project}")
    if transcript.error:
        print(f"[record] WARNING: recording stopped early ({transcript.error}) — the replay will "
              "be truncated.")


def _configure_logging() -> None:
    """Show the agents' live progress ([PLANNING]/[GENERATING]/[DONE]) in this terminal.

    This script drives the graph directly (it never imports app.main), so nothing has configured
    the root logger yet — without this, Python suppresses INFO lines.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _run(args: argparse.Namespace, *, mode: str, do_publish: bool,
         make_public: bool) -> dict[str, Any] | None:
    """Build (or resume) one run; returns its final WorkflowState, or None when nothing ran."""
    if args.resume:
        return _resume(args, mode=mode, make_public=make_public)

    # Every fresh (non --resume) invocation below calls new_state() + workflow.invoke(initial,
    # config) with thread_id == --project. new_state() only sets ~20 core fields and leaves
    # downstream ones (security_verdict, unit_tests, repo_url, review_report, ...) UNSET; LangGraph
    # merges an invoke() input onto the EXISTING checkpoint for that thread_id rather than clearing
    # it, so any field new_state() omits silently keeps its value from a PRIOR run under this same
    # --project name. Concretely: a stale security_verdict makes route_after_refactoring (which
    # tells its two callers apart ONLY via "security_verdict" in state) skip the entire
    # Debugging<->Unit-Test<->Documentation phase on the very first pass. The only safe paths once
    # a checkpoint exists are --resume (continues the SAME state) or a --project name that has
    # never run before.
    if args.fresh:
        # Resolved BEFORE the guard below: picking an unused name is exactly what that guard asks
        # the operator to do, so with --fresh there is nothing left for it to abort on.
        args.project = _fresh_project_name(
            args.project,
            out_base=(args.out_dir or _DEFAULT_OUT_DIR).resolve(),
            owner=_resolve_owner(args.owner) if do_publish else "",
            check_github=do_publish and args.repo_name is None,
        )
    _existing_thread_state = workflow.get_state(
        {"configurable": {"thread_id": args.project}}
    ).values
    if _existing_thread_state:
        print(
            f"A checkpoint already exists for --project {args.project!r} "
            f"(workflow_status={_existing_thread_state.get('workflow_status')!r}) in "
            f"{resolve_checkpoint_db_path()!r}. Starting a FRESH run under this same name would "
            f"silently inherit stale fields (security_verdict, unit_tests, repo_url, ...) from "
            f"that prior run and can skip whole phases of the pipeline without any error.\n\n"
            f"Pick one:\n"
            f"  --resume                    continue that run from its last completed node\n"
            f"  --project <a-new-name>       start a genuinely fresh run\n"
            f"  --fresh                     keep this name; the run picks an unused variant\n\n"
            f"If this checkpoint is unexpected (e.g. you didn't think {args.project!r} had run "
            f"before), the file above is checked regardless of which directory this script is "
            f"launched from — delete it directly if it's stale.\n\n"
            f"Aborting — no work done."
        )
        return

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

    return _report(state, args, executor, push_enabled=push_enabled, git_remote=git_remote)


def _resume(args: argparse.Namespace, *, mode: str, make_public: bool) -> dict[str, Any] | None:
    """Continue a run that crashed mid-graph, from its last completed node.

    Needs no pack/work_items — the checkpointed ``WorkflowState`` already has them; only the
    executor is process-local and must be reconnected to the SAME product repo before the graph
    can proceed (``workflow.invoke(None, config)`` resumes exactly where the last completed
    checkpoint left off — see ``app/graph/graph.py``'s SQLite checkpointer).
    """
    run_id = args.project
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 1000}
    existing = workflow.get_state(config).values
    if not existing:
        # The RESOLVED path (see resolve_checkpoint_db_path), not the raw get_settings() string —
        # the configured value is commonly a relative path, and printing it as-is misrepresents
        # where this actually looked: sqlite3.connect resolves a relative path against the
        # process's CWD, which is not necessarily where the reader of this message is standing.
        print(
            f"--resume: no checkpoint found for project {run_id!r} (thread_id={run_id!r}) in "
            f"{resolve_checkpoint_db_path()!r}. Either this project never ran with the SQLite "
            f"checkpointer, or the run already finished and its checkpoint was read once already. "
            f"Nothing to resume — start a fresh run instead."
        )
        return
    print(
        f"--resume: found checkpoint for {run_id!r} (workflow_status="
        f"{existing.get('workflow_status')!r}, {len(existing.get('generated_code', []))} file(s) "
        f"on record) — reconnecting the executor and continuing from the last completed node."
    )

    executor: Executor
    if mode == "dry-run":
        print("--resume: --dry-run keeps files in-memory only (FakeExecutor) — nothing to "
              "reconnect to across a process restart. Re-run without --resume instead.")
        return None
    if mode == "sandbox":
        executor = asyncio.run(MCPExecutor.connect(args.sandbox_url))
    else:
        out_base = (args.out_dir or _DEFAULT_OUT_DIR).resolve()
        executor = LocalDiskExecutor(out_base, private=not make_public)
        print(f"Reconnected to product repo at {out_base / args.project}")
    set_executor(executor)

    workflow.invoke(None, config)
    state = workflow.get_state(config).values

    return _report(
        state, args, executor,
        push_enabled=bool(existing.get("push_enabled")), git_remote=existing.get("git_remote") or "",
    )


def _report(
    state: dict[str, Any], args: argparse.Namespace, executor: Executor, *,
    push_enabled: bool, git_remote: str,
) -> dict[str, Any]:
    """Shared tail: print the run's outcome, dump in-memory files (dry-run), and report publish status.

    Returns ``state`` so the caller can record the run's real artifacts in the transcript manifest.
    """
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
