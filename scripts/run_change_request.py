"""Implement a CHANGE REQUEST against an EXISTING repository (brownfield mode).

The counterpart to ``run_fixture.py``: that one takes a design pack and builds a new application,
this one takes a repo URL plus a description of a change and works on the code that is already
there. Same compiled graph, different entry lane — ``router.route_entry`` sends a run with
``source_mode="brownfield"`` to ``acquire_repo_node`` instead of ``scaffold_node``.

Deliberately THINNER than run_refactoring.py: it does not clone. ``acquire_repo_node`` owns
acquisition (URL allowlist -> clone -> resolve the real default branch -> pin the base commit ->
cut a per-run branch -> survey -> baseline digests), so the same preconditions apply however the
run is started, rather than living in whichever script happened to launch it.

    python scripts/run_change_request.py <github_repo_url> --change-request <file.md|.json> \\
        [--project NAME] [--run-id ID] [--base-ref BRANCH_OR_SHA] [--workspace DIR]

This is the whole loop: clone -> survey -> plan -> validate the plan -> edit -> PROVE the edit
happened and nothing else did -> re-run the repo's OWN tests -> commit -> (with --push) open a
DRAFT pull request.

Only a REGRESSION blocks: a suite that passed before the change and fails after it. A suite
that was already failing, or that cannot run here at all (a missing dev dependency is the
usual reason), does not block — but then the change is reported as NOT test-verified rather
than quietly treated as green.

**--push is opt-in.** Without it the run edits and commits LOCALLY and opens no PR, so nothing
reaches the target repository and the diff is still there to read in the workspace. The shared
quality pipeline (code review / refactoring / debug+test / security) does not run on this lane yet.

The planner REFUSING is also a success. It is instructed to return no plan, with its reasoning,
when a request needs a rename or signature change (this pipeline cannot find a symbol's callers),
is cross-cutting, is a migration, or is too vague to locate in the code. A stated refusal is a far
better outcome than a plausible-looking plan for files it never read.

The change-request file is either JSON with the fields below, or plain Markdown - in which case
the first heading becomes the title and the rest the description:

    {"id": "CR-1", "title": "...", "kind": "bug|feature|modification",
     "description": "...", "acceptance_criteria": ["..."], "constraints": ["..."]}

Requires: git on PATH, a PUBLIC https://github.com/<owner>/<repo> URL (the clone allowlist), and
ANTHROPIC_FOUNDRY_* credentials in .env for the planning call. No Docker.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

# Make `app...` importable when run as `python scripts/run_change_request.py` from the service root.
_IMPL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_IMPL_DIR))
sys.path.insert(0, str(_IMPL_DIR / "scripts"))


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-5s  %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "urllib3", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _make_console_safe() -> None:
    """Never let printing MODEL OUTPUT kill the run.

    Most of what this script prints — the planner's notes, a change_intent, a rejection reason —
    was written by the LLM, and a Windows console defaults to cp1252, which cannot encode most of
    what a model reaches for (an em dash, ``≤``, a curly quote). ``print`` then raises
    UnicodeEncodeError and takes down a run whose actual work already completed. Replacing the
    unencodable character is always better than losing the result.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):  # a redirected/wrapped stream may not support it
            pass


from app.graph.graph import workflow  # noqa: E402
from app.graph.state import new_state  # noqa: E402
from app.integrations.executor import set_executor  # noqa: E402
from local_executor import LocalDiskExecutor  # noqa: E402

_KINDS = ("bug", "feature", "modification")


def _load_change_request(path: Path) -> dict[str, Any]:
    """Parse a change request from JSON or Markdown.

    Markdown is accepted because that is how a change request actually arrives (an issue body, a
    ticket export). Requiring JSON would mean every caller hand-converting first, and a
    hand-converted description is one more place to drop the acceptance criteria.
    """
    # utf-8-SIG, not utf-8: PowerShell's `Set-Content -Encoding utf8` (the obvious way to write this
    # file on Windows, and what our own instructions recommend) emits a UTF-8 BOM. Read as plain
    # utf-8 the BOM survives as a leading ﻿, `.lstrip()` does not remove it (it is not
    # whitespace), the "# Heading" test fails, and the title silently degrades to the filename —
    # which then propagates into the commit subject and the pull request title. json.loads chokes on
    # it outright. utf-8-sig strips a BOM when present and is a no-op when it is not.
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a JSON object, got {type(data).__name__}")
    else:
        lines = text.splitlines()
        first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
        if first is not None and lines[first].lstrip().startswith("#"):
            # Promote the heading to the title and drop it from the body, or every report renders
            # the same title twice (once as its own heading, once at the top of the description).
            title = lines[first].lstrip("# ").strip()
            body = "\n".join(lines[first + 1:]).strip()
        else:
            title, body = path.stem, text.strip()
        data = {"title": title, "description": body}

    data.setdefault("id", path.stem)
    data.setdefault("kind", "modification")
    data.setdefault("title", data["id"])
    data.setdefault("description", "")
    data.setdefault("acceptance_criteria", [])
    data.setdefault("constraints", [])
    if data["kind"] not in _KINDS:
        raise ValueError(f"{path}: kind must be one of {_KINDS}, got {data['kind']!r}")
    if not str(data["description"]).strip():
        raise ValueError(f"{path}: the change request has no description - nothing to implement")
    return data


def main() -> int:
    # Before parse_args: argparse prints --help and exits from INSIDE it, so a later call would
    # leave the help text itself (which contains non-ASCII) to be mangled by a cp1252 console.
    _make_console_safe()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo_url", help="PUBLIC https://github.com/<owner>/<repo> to modify")
    parser.add_argument("--change-request", "--cr", dest="change_request", required=True,
                        help="path to the change request (.json or .md)")
    parser.add_argument("--project", default=None,
                        help="project name (default: derived from the repo URL)")
    parser.add_argument("--run-id", default=None, help="run id (default: a random 8-hex id)")
    parser.add_argument("--base-ref", default="",
                        help="branch/tag/sha to start from (default: the repo's default branch)")
    parser.add_argument("--workspace", default=None,
                        help="where to clone (default: <impl>/workspace/<project>-<run>)")
    parser.add_argument("--push", action="store_true",
                        help="push the change branch and open a DRAFT pull request. OFF by "
                             "default: without it the run edits and commits LOCALLY only, so "
                             "nothing reaches the target repository and the diff is still "
                             "inspectable in the workspace")
    parser.add_argument("--token", default=None,
                        help="GitHub PAT for the push (default: $GITHUB_PAT, then the gh CLI)")
    args = parser.parse_args()
    _setup_logging()

    try:
        change_request = _load_change_request(Path(args.change_request))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    project_id = args.project or re.sub(
        r"[^A-Za-z0-9._-]", "-", args.repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    )
    run_id = args.run_id or uuid.uuid4().hex[:8]
    workspace = Path(args.workspace) if args.workspace else _IMPL_DIR / "workspace" / f"{project_id}-{run_id}"

    executor = LocalDiskExecutor(workspace)
    set_executor(executor)

    print("=" * 72)
    print("  CHANGE REQUEST (brownfield)")
    print("=" * 72)
    print(f"  Repo      : {args.repo_url}")
    print(f"  Request   : {change_request['id']} - {change_request['title']}")
    print(f"  Kind      : {change_request['kind']}")
    print(f"  Push      : {'YES - will open a DRAFT PR' if args.push else 'no (local commit only)'}")
    print(f"  Workspace : {workspace / project_id}")
    print("=" * 72)

    # The push target is the repo we cloned — never a new one. `git_remote` is the owner/name the
    # publish helpers use; parsing it from the URL means a run can never push somewhere else.
    remote = ""
    if args.push:
        match = re.match(r"^https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", args.repo_url)
        if not match:
            print(f"ERROR: --push needs a https://github.com/<owner>/<repo> URL, got {args.repo_url}",
                  file=sys.stderr)
            return 2
        remote = f"{match.group(1)}/{match.group(2)}"

    state = new_state(
        run_id=run_id, attempt=0, project_id=project_id,
        source_mode="brownfield", source_repo_url=args.repo_url,
        change_request=change_request, base_ref=args.base_ref,
        push_enabled=bool(args.push), git_remote=remote,
        git_token=(args.token or os.environ.get("GITHUB_PAT") or ""),
    )
    # A per-run thread_id, never the project name: LangGraph MERGES an invoke() input onto whatever
    # checkpoint already exists for the id, so reusing a name silently inherits the previous run's
    # fields (a stale security_verdict, for one, reroutes the graph past the whole verify phase).
    config = {"configurable": {"thread_id": f"{project_id}-{run_id}"}, "recursion_limit": 1000}

    try:
        workflow.invoke(state, config)
    except Exception as exc:  # noqa: BLE001 - report the failure, don't dump a traceback at a user
        logging.exception("brownfield run failed")
        print(f"\nERROR: the run failed: {exc}", file=sys.stderr)
        return 1

    final = workflow.get_state(config).values
    inv = final.get("repo_inventory") or {}
    items = final.get("work_items") or []
    errors = final.get("change_plan_errors") or []
    impacts = final.get("change_impacts") or {}

    print("\n" + "=" * 72)
    print("  ACQUISITION COMPLETE" if inv else "  ACQUISITION FAILED")
    print("=" * 72)
    print(f"  Base branch : {final.get('base_branch') or '-'}")
    print(f"  Base commit : {(final.get('base_sha') or '-')[:12]}")
    print(f"  Work branch : {final.get('branch') or '-'}")
    if inv:
        print(f"  Files       : {len(inv.get('files', []))} "
              f"({len(inv.get('source_files', []))} source, {len(inv.get('test_files', []))} test)")
        print(f"  Modules     : {len(inv.get('by_dir', {}))}")
        print(f"  Language    : {inv.get('primary_language') or 'unknown'}")
    print(f"  Report      : {final.get('change_report_path') or '(not saved)'}")
    print(f"  Status      : {final.get('workflow_status')}")
    print("=" * 72)

    if not inv:
        print((final.get("generation_summary") or "").strip())
        return 1

    print("\n" + "=" * 72)
    print("  PLAN ACCEPTED" if items else "  NO PLAN")
    print("=" * 72)
    for item in items:
        print(f"  [{item.action}] {item.id}")
        print(f"      {item.change_intent}")
        for path in item.target_files:
            impacted = impacts.get(path) or []
            blast = f"  <- imported by {len(impacted)}: {', '.join(impacted[:3])}" if impacted else ""
            print(f"      - {path}{blast}")
    if errors:
        print("  REJECTED because:")
        for err in errors:
            print(f"      - {err}")
    if notes := (final.get("change_plan_notes") or "").strip():
        print(f"\n  Planner notes: {notes}")
    print("=" * 72)

    changed = final.get("changed_files") or []
    rel = [p[len(project_id) + 1:] if p.startswith(f"{project_id}/") else p for p in changed]
    print("\n" + "=" * 72)
    print("  CHANGE APPLIED" if changed else "  NO CHANGE APPLIED")
    print("=" * 72)
    for path in rel:
        print(f"      M {path}")
    if notes := (final.get("modifier_notes") or "").strip():
        print(f"\n  What it did: {notes}")
    verify = final.get("verify_test") or final.get("baseline_test") or {}
    print(f"\n  Tests       : {final.get('verify_verdict') or '-'} "
          f"({verify.get('summary') or 'not run'})")
    if final.get("verify_verdict") == "unverified":
        print("                ^ NOT verified - the suite could not run, so nothing was proven")
    print(f"  Commit      : {final.get('workflow_status')}")
    print(f"  PR          : {final.get('pr_url') or final.get('finalize_status') or '-'}")
    print("=" * 72)
    print((final.get("generation_summary") or "").strip())

    if changed:
        print(f"\nReview the diff:  git -C \"{workspace / project_id}\" show HEAD")
        if not args.push:
            print("Nothing was pushed - this was a local run. Re-run with --push to open a draft PR.")
    return 0 if changed else 1


if __name__ == "__main__":
    raise SystemExit(main())
