"""LangGraph node functions.

Each node wraps one step of the IMP-001 subgraph. Agents are instantiated once at import and
reused. The executor is resolved at run time via the provider (``get_executor``), so the same
node code works with the real MCP sandbox (set in the app lifespan) or a FakeExecutor (set in
tests).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from app.agents.change_planner import ChangePlannerAgent
from app.agents.code_generator import CodeGeneratorAgent
from app.agents.code_modifier import CodeModifierAgent
# _slug is the report-folder naming convention Code Review and Refactoring already share; the
# change report lands in the SAME reports/<project>-<run>/ folder, so it reuses it rather than
# adding a third copy of the same regex that could drift out of step with them.
from app.agents.code_review import CodeReviewAgent, _slug
from app.agents.documentation import DocumentationAgent
from app.agents.security import SecurityAgent
from app.agents.unit_test import UnitTestAgent
from app.config.settings import get_settings
from app.graph.state import GateCheck, WorkflowState
from app.integrations.executor import get_executor
from app.integrations.github import get_github_client
from app.integrations.review_sandbox import is_allowed_repo_url
from app.services.boilerplate import render_scaffold
from app.services.change_gate import classify_regressions, evaluate
from app.services.change_plan import normalize_target, render_plan, validate_plan
from app.services.packaging import build_project_zip
from app.services.test_command import detect as detect_test_command
from app.services.test_command import run as run_tests
from app.services.repo_inventory import (
    RepoInventory,
    detect_default_branch,
    digests,
    inventory,
)
from app.services.wiring import (
    build_import_graph,
    dependents_of,
    find_unresolved_imports,
    reconcile_wiring,
)

logger = logging.getLogger(__name__)

_change_planner = ChangePlannerAgent()
_code_generator = CodeGeneratorAgent()
_code_modifier = CodeModifierAgent()
_code_review = CodeReviewAgent()
_unit_test_agent = UnitTestAgent()
_documentation_agent = DocumentationAgent()
_security_agent = SecurityAgent()

# owner/repo out of the same https://github.com/<owner>/<repo> form `is_allowed_repo_url` accepts.
_OWNER_REPO_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def _stage(agent: str, doing: str) -> None:
    """Emit a clear, greppable banner so the terminal shows WHICH agent/step is running and WHAT
    it is doing. ASCII-only (Windows consoles mangle non-ASCII), two lines: a named banner + the
    action. Every node calls this first; the agent's own INFO lines then fill in the sub-steps.
    """
    logger.info("================ AGENT: %s ================", agent)
    logger.info("   -> %s", doing)


def _note(state: WorkflowState, line: str) -> WorkflowState:
    """Append one line to the run summary — the artifact a human actually reads after a run.

    A stage outcome that lives only in a log line is effectively invisible (a failed finalize used
    to end a run that still reported "completed" with a zip). Returns ``state`` so nodes can
    ``return _note(state, ...)``.
    """
    state["generation_summary"] = (state.get("generation_summary") or "") + line.rstrip("\n") + "\n"
    return state


def scaffold_node(state: WorkflowState) -> WorkflowState:
    """FIXED, deterministic: render the repo-root boilerplate once, before any work item.

    No LLM — Jinja2 templates only (app/services/boilerplate.py). Runs exactly once per run,
    so requirements.txt/package.json exist before the first work item's build check runs. The
    scaffold is INPUT-AWARE: the Design Package's capabilities config decides which files are
    emitted and their contents (absent that config, the legacy FastAPI+React defaults apply).
    """
    _stage("Scaffold (boilerplate)", "rendering project boilerplate (Dockerfile, requirements, "
           "package.json, ...) and, if publishing, creating the repo + pushing 'main'")
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    files = render_scaffold(project_dir, state.get("design_package"))
    generated = list(state.get("generated_code", []))
    scaffold_files = list(state.get("scaffold_files", []))
    written: list[str] = []
    for entry in files:
        path = f"{project_dir}/{entry['path']}"
        executor.write_file(path, entry["content"])
        written.append(path)
        generated.append(path)
        scaffold_files.append(entry["path"])  # repo-root-relative — used for the main-branch commit
    state["generated_code"] = generated
    state["scaffold_files"] = scaffold_files
    names = ", ".join(w.rsplit("/", 1)[-1] for w in written)
    state["generation_summary"] = (
        state.get("generation_summary") or ""
    ) + f"[scaffold] rendered {len(written)} boilerplate file(s): {names}\n"

    # Incremental live publish: push the scaffold to 'main' NOW (creating the GitHub repo) so the
    # repo appears BEFORE any feature is generated, and record repo_url for the inline Code Review.
    # Only when push is enabled AND the executor supports it (local-disk); otherwise unchanged.
    push = bool(state.get("push_enabled")) and bool(state.get("git_remote"))
    if push and hasattr(executor, "publish_scaffold"):
        remote = state["git_remote"]
        try:
            res = executor.publish_scaffold(
                project_dir, scaffold_files, remote=remote, token=state.get("git_token") or None
            )
        except Exception as exc:  # noqa: BLE001 - a publish failure must never crash the run
            logger.exception("scaffold publish failed for run %s", state.get("run_id"))
            state["generation_summary"] += f"[publish] scaffold push FAILED: {exc}\n"
        else:
            state["repo_url"] = _repo_url_from_remote(remote)
            ok = getattr(res, "exit_code", 1) == 0
            logger.info(
                "[publish] repo live + 'main' pushed: %s (%s)",
                state["repo_url"], "ok" if ok else "PUSH FAILED",
            )
            state["generation_summary"] += (
                f"[publish] repo live at {state['repo_url']} — scaffold pushed to 'main'"
                + ("" if ok else " (PUSH FAILED)") + "\n"
            )
    return state


def code_generator_node(state: WorkflowState) -> WorkflowState:
    """LLM: generate + write files for the current work item (no gate/commit here)."""
    wi = state.get("current_work_item")
    label = getattr(wi, "id", "?") if wi is not None else "?"
    _stage("Code Generator", f"generating source files for work item {label}")
    return _code_generator.execute(state)


def code_review_node(state: WorkflowState) -> WorkflowState:
    """Clone the committed repo into an ephemeral sandbox, run static analysis, write the report.

    The agent owns the whole sandbox session (clone → ruff/eslint → sonar-scanner → teardown);
    this node just delegates. Runs ONCE, right after the run-level commit and BEFORE Refactoring
    and the Debugging<->Unit-Test loop (every escalate branch in the code-generation loop bypasses
    it). Needs ``repo_url`` in state to clone; when absent the agent writes a report noting no repo.
    Stamps ``workflow_status = "code_reviewed"`` — an intermediate marker, later superseded by
    Refactoring and the debug/test loop (Unit Testing sets the terminal ``"completed"``).
    """
    _stage("Code Reviewer", "cloning the pushed repo, running ruff / eslint / sonar-scanner, "
           "aggregating findings, writing the report")
    return _code_review.execute(state)


def refactoring_publish_node(state: WorkflowState) -> WorkflowState:
    """FIXED commit + push of the Refactoring agent's edits to the working branch ('dev').

    Runs right after Refactoring and BEFORE the debug/test loop, so the fixed files land on the
    remote 'dev' branch as soon as they exist in the sandbox — the Debugging agent (and anything
    else downstream) can then fetch the refactored code from GitHub instead of relying only on
    the shared sandbox workspace. Never formed by the LLM (CLAUDE.md rule 2) — the agent records
    WHAT it edited (``refactored_files``); this deterministic node does the git work.

    Three shapes, mirroring ``feature_publish_node`` / ``commit_node``:
    * Nothing edited (clean review / early exit) → pass through, no commit (keeps the graph
      acceptance tests' "committed exactly once" invariant on runs where refactoring is a no-op).
    * Push enabled AND the executor supports incremental publish (``publish_feature``, the
      local-disk executor) → ONE ``refactor(...)`` commit of exactly the edited paths on the
      working branch, pushed to the remote.
    * Otherwise (sandbox/test path) → a plain fixed-path ``git_commit`` so the refactor is at
      least recorded in the workspace repo; no push is available there.

    A publish/commit failure is logged + noted in ``generation_summary`` — never crashes the run
    (the debug/test loop still verifies the refactored code from the shared workspace).
    """
    touched = [p for p in (state.get("refactored_files") or []) if p]
    if not touched:
        return state  # nothing refactored -> nothing to publish

    _stage("Refactoring Publish", "committing the refactored files and pushing 'dev' so the "
           "Debugging agent can pull them from the remote")
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    branch = (state.get("branch") or get_settings().working_branch or "").strip() or "dev"
    message = f"refactor({state.get('run_id') or 'run'}): apply code review fixes to {len(touched)} file(s)"
    # refactored_files are project-prefixed (like generated_code); publish_feature stages paths
    # relative to project_dir, so strip the prefix.
    rel_paths = [p[len(project_dir) + 1:] if p.startswith(f"{project_dir}/") else p for p in touched]
    push = bool(state.get("push_enabled")) and bool(state.get("git_remote"))

    try:
        if push and hasattr(executor, "publish_feature"):
            res = executor.publish_feature(
                project_dir, message, rel_paths,
                feature_branch=branch, token=state.get("git_token") or None,
            )
            ok = getattr(res, "exit_code", 1) == 0
            logger.info("[publish] refactoring fixes pushed to '%s': %s (%s)",
                        branch, message, "ok" if ok else "PUSH FAILED")
            state["generation_summary"] = (state.get("generation_summary") or "") + (
                f"[publish] {message} pushed to '{branch}'" + ("" if ok else " (PUSH FAILED)") + "\n"
            )
        else:
            res = executor.git_commit(project_dir, message)  # LLM never forms/executes this (rule 2)
            ok = bool(getattr(res, "committed", False))
            if not ok:
                logger.warning(
                    "[publish] refactoring local commit FAILED for run %s: %s",
                    state.get("run_id"),
                    (getattr(res, "stderr", "") or getattr(res, "stdout", "")).strip()[:200],
                )
            state["generation_summary"] = (state.get("generation_summary") or "") + (
                f"[publish] {message} committed locally (push not available)"
                + ("" if ok else " (COMMIT FAILED)") + "\n"
            )
    except Exception as exc:  # noqa: BLE001 - a publish failure must never crash the run
        action = "push" if (push and hasattr(executor, "publish_feature")) else "local commit"
        logger.exception("refactoring %s failed for run %s", action, state.get("run_id"))
        state["generation_summary"] = (state.get("generation_summary") or "") + (
            f"[publish] refactoring {action} FAILED: {exc}\n"
        )
    return state


def select_work_item_node(state: WorkflowState) -> WorkflowState:
    """Advance to the next unit of work; reset the LOCAL repair counter.

    Walks the ``work_items`` cursor one item at a time. When the plan is exhausted it clears
    ``current_work_item`` so the run proceeds straight to the auto-commit (no batch-review /
    rework queue — HITL was removed).
    """
    items = state.get("work_items", [])
    if not isinstance(items, list):  # fail fast on malformed input, don't crash mid-loop
        raise ValueError(f"work_items must be a list, got {type(items).__name__}")
    index = int(state.get("work_item_index", 0))
    if index < len(items):
        state["current_work_item"] = items[index]
        state["work_item_index"] = index + 1
        state["repair_attempt"] = 0  # LOCAL, reset per work item (never touches `attempt`)
    else:
        state["current_work_item"] = None  # plan exhausted -> auto-commit
    return state


def feature_publish_node(state: WorkflowState) -> WorkflowState:
    """Incremental live publish of the just-gate-passed work item: commit its files to 'dev' and
    push, so the repo fills in as it is generated (per-work-item, not batched at the end).

    Commit granularity: this path is **one commit per work item** by design — the deliberate
    trade-off for live streaming. That diverges from the batch path (``commit_node`` ->
    ``_group_feature_commits``), which does ONE commit per user-feature (rule 6). A feature that
    spans several work items therefore lands as several ``dev`` commits here; ``_item_commit_message``
    keeps them distinct by tagging each with the work-item id.

    No-op unless push is enabled AND the executor supports incremental publish (the local-disk
    executor). For the sandbox/test path it passes straight through, and the single end-commit in
    ``commit_node`` still handles committing — so existing behavior is unchanged there.
    """
    executor = get_executor()
    push = bool(state.get("push_enabled")) and bool(state.get("git_remote"))
    work_item = state.get("current_work_item")
    if not (push and work_item is not None and hasattr(executor, "publish_feature")):
        return state
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    # Honour state["branch"] like refactoring_publish_node does, instead of letting publish_feature
    # fall back to its own "dev" default: the run's working branch is a state field, and a caller
    # that set it was previously ignored here while being respected two nodes later.
    branch = (state.get("branch") or get_settings().working_branch or "").strip() or "dev"
    message = _item_commit_message(work_item)
    try:
        res = executor.publish_feature(
            project_dir, message, list(work_item.target_files),
            feature_branch=branch, token=state.get("git_token") or None,
        )
    except Exception as exc:  # noqa: BLE001 - a publish failure must never crash the run
        logger.exception("feature publish failed for run %s", state.get("run_id"))
        state["generation_summary"] = (state.get("generation_summary") or "") + f"[publish] feature push FAILED: {exc}\n"
        return state
    ok = getattr(res, "exit_code", 1) == 0
    logger.info("[publish] feature pushed to '%s': %s (%s)", branch, message, "ok" if ok else "PUSH FAILED")
    state["generation_summary"] = (state.get("generation_summary") or "") + (
        f"[publish] {message} pushed to '{branch}'" + ("" if ok else " (PUSH FAILED)") + "\n"
    )
    return state


#: Clone timeout. A large repo over a slow link legitimately takes minutes; the default 120s
#: run_command timeout would abort those runs before any work started.
_CLONE_TIMEOUT = 600.0

#: Switches that make git fail instead of blocking. ``GIT_TERMINAL_PROMPT=0`` is the important one:
#: without it, a URL that passes the allowlist but is private or misspelled makes git BLOCK on a
#: username prompt against a dead stdin until the timeout — a run that hangs instead of failing.
#: ``GIT_ASKPASS`` closes the same door for the credential-helper/GUI path.
_GIT_NONINTERACTIVE = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo", "GCM_INTERACTIVE": "never"}


def _git_env() -> dict[str, str]:
    """The non-interactive switches LAYERED ON TOP of the real environment.

    ``env=`` REPLACES the child's environment rather than extending it, so handing subprocess a
    bare dict of switches strips PATH, SystemRoot and everything else — git then fails to resolve
    DNS at all ("Could not resolve host: github.com"), which reads exactly like a network outage
    rather than a caller bug. ``local_executor._pat_env`` merges for the same reason.
    """
    return {**os.environ, **_GIT_NONINTERACTIVE}


def _acquire_failed(state: WorkflowState, reason: str) -> WorkflowState:
    """Abandon acquisition with a stated reason. Nothing has been written to any remote at this
    point, so failing here is always safe — and it is where EVERY brownfield precondition is
    checked, so a bad request can never reach a node that edits or pushes."""
    logger.warning("[acquire] %s", reason)
    state["workflow_status"] = "needs_human_review"
    return _note(state, f"[acquire] FAILED - {reason}")


def acquire_repo_node(state: WorkflowState) -> WorkflowState:
    """FIXED, deterministic: clone the EXISTING repo a change request targets, and survey it.

    The brownfield counterpart to ``scaffold_node``, and deliberately a SEPARATE node rather than a
    flag on it. ``scaffold_node`` unconditionally overwrites .gitignore, README.md, package.json /
    requirements.txt, Dockerfile, docker-compose.yml and the jest/babel configs from
    ``DEFAULT_CAPABILITIES`` (never reading disk), then calls ``publish_scaffold`` -> ``git checkout
    -B main`` + ``gh repo create`` + ``git push -u origin main``. Against a clone that push is a
    clean FAST-FORWARD onto the user's default branch: their manifests replaced, committed, pushed,
    no PR, no review. A guard inside that node would leave the landmine armed for whoever adds the
    next write; routing around it cannot regress.

    Writes nothing to the remote and nothing into the working tree - it clones, branches locally,
    and records what it found.
    """
    _stage("Acquire Repo", "cloning the target repository and surveying what it contains")
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    repo_url = (state.get("source_repo_url") or "").strip()

    if not repo_url:
        return _acquire_failed(state, "no source_repo_url was provided")
    # Validate BEFORE any network call. The in-graph sandbox clones were always allowlisted, but the
    # host clone below runs with the operator's own git/gh credentials, and until now nothing ever
    # handed it a caller-supplied URL. `git clone <anything>` with real credentials is exactly the
    # SSRF-shaped primitive review_sandbox's allowlist exists to prevent.
    if not is_allowed_repo_url(repo_url):
        return _acquire_failed(
            state,
            f"'{repo_url}' is not an allowed repository URL (expected a public "
            "https://github.com/<owner>/<repo>)",
        )
    if not state.get("change_request"):
        return _acquire_failed(state, "no change_request was provided - nothing to implement")
    # The exec-sandbox has no egress to github.com by design (tools/exec-sandbox/squid.conf), so it
    # can neither clone the target nor push the result. Fail at the boundary rather than 40 minutes
    # into a run. `publish_feature` is the marker for the local-disk executor.
    if not hasattr(executor, "publish_feature"):
        return _acquire_failed(
            state,
            "the active executor cannot reach GitHub (the exec-sandbox has no egress to it) - "
            "brownfield runs need the local-disk executor",
        )

    clone = executor.run_command(
        ["git", "clone", "--no-recurse-submodules", "--", repo_url, "."],
        cwd=project_dir, timeout=_CLONE_TIMEOUT, env=_git_env(),
    )
    if clone.exit_code != 0:
        detail = (clone.stderr or clone.stdout or "").strip()[:300]
        return _acquire_failed(state, f"git clone failed: {detail or 'no output'}")

    base_branch = detect_default_branch(executor, project_dir)
    state["base_branch"] = base_branch

    base_ref = (state.get("base_ref") or "").strip()
    if base_ref:
        checkout = executor.run_command(
            ["git", "checkout", base_ref], cwd=project_dir, env=_git_env()
        )
        if checkout.exit_code != 0:
            detail = (checkout.stderr or checkout.stdout or "").strip()[:200]
            return _acquire_failed(state, f"could not check out base_ref '{base_ref}': {detail}")

    head = executor.run_command(["git", "rev-parse", "HEAD"], cwd=project_dir)
    state["base_sha"] = head.stdout.strip() if head.exit_code == 0 else ""

    # A dedicated per-run branch, never the repo's default and never `dev`: on a real repo `dev` is
    # often a shared integration branch, so reusing it would both land commits on someone's shared
    # work and make finalize's PR diff every unrelated commit already on it rather than this change.
    work_branch = f"sdlc/cr-{state.get('run_id') or 'run'}"
    branched = executor.run_command(
        ["git", "checkout", "-b", work_branch], cwd=project_dir, env=_git_env()
    )
    if branched.exit_code != 0:
        detail = (branched.stderr or branched.stdout or "").strip()[:200]
        return _acquire_failed(state, f"could not create working branch '{work_branch}': {detail}")
    state["branch"] = work_branch

    inv = inventory(executor, project_dir)
    if inv.is_empty:
        return _acquire_failed(state, "the cloned repository contains no readable files")
    state["repo_inventory"] = inv.as_dict()
    state["baseline_digests"] = digests(executor, project_dir, inv.files)

    # Run the repo's OWN suite before touching anything. Without this baseline a post-change
    # failure cannot be attributed: a repo is not obliged to be green when we find it, and blaming
    # the change for a test that was already red would send the run off fixing code it never wrote.
    _record_baseline_tests(state, executor, project_dir, inv.as_dict())

    # repo_url is what every downstream node already reads (code_review/security clone it, finalize
    # parses owner/repo out of it). Copying the input across here is the ONE authorised crossing
    # point between the two fields, so `repo_url` keeps its "this is the repo we work on" meaning
    # without source_repo_url losing its "this one was handed to us" meaning.
    state["repo_url"] = repo_url
    # generated_code stays the CHANGE SET, not the repo. Its own contract is "files written this
    # run", and documentation_node reads every entry into one prompt while packaging zips them —
    # seeding a 2,000-file repo here would blow up both.
    state["generated_code"] = []
    state["workflow_status"] = "repo_acquired"

    logger.info(
        "[acquire] %s @ %s (base=%s, branch=%s) | %d files, %d source, %d modules, primary=%s",
        repo_url, (state["base_sha"] or "?")[:8], base_branch, work_branch,
        len(inv.files), len(inv.source_files), len(inv.by_dir), inv.primary_language or "unknown",
    )
    _write_change_report(state, inv)
    return _note(
        state,
        f"[acquire] cloned {repo_url} @ {(state['base_sha'] or '?')[:8]} "
        f"(base '{base_branch}', working on '{work_branch}') - "
        f"{len(inv.files)} file(s), {len(inv.source_files)} source, {len(inv.by_dir)} module(s)",
    )


#: Cap on how many source files are read to build the reverse import graph. Reading is per-file, so
#: an unbounded graph over a large monorepo would dominate the run. Overflow is reported, not
#: silently ignored — a partial graph means "dependents may be under-reported", and whoever reviews
#: the pull request needs to know that rather than trusting an incomplete blast radius.
_MAX_GRAPH_FILES = 800


def change_plan_node(state: WorkflowState) -> WorkflowState:
    """LLM: decide which files the change request must touch — then CHECK that answer.

    Three steps, deliberately in this order: the planner proposes, ``change_plan.validate_plan``
    checks the proposal against the repository that was actually cloned, and the reverse import
    graph records what else imports the files it wants to touch. Validation runs before anything
    downstream sees the plan, because the failure mode is quiet: a plan naming plausible files the
    planner never opened is indistinguishable from a good one, and every later step trusts it.

    A rejected plan clears ``work_items`` (which is what routes the run to escalate) but KEEPS the
    plan and the reasons in the report — a rejected plan is the most useful thing to hand a human.
    """
    _stage("Change Planner", "deciding which files the change request must touch, then validating "
           "that plan against the repository")
    state = _change_planner.execute(state)

    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    inv = state.get("repo_inventory") or {}
    items = list(state.get("work_items") or [])

    # The planner already failed/refused — nothing to validate, and its reason is already recorded.
    if not items:
        state["change_plan_errors"] = state.get("change_plan_errors") or []
        _append_plan_to_report(state, items, state["change_plan_errors"], {})
        return state

    _, errors = validate_plan(items, inv.get("files") or [])
    state["change_plan_errors"] = errors

    impacts = _blast_radius(state, executor, project_dir, inv, items)
    state["change_impacts"] = impacts

    if errors:
        logger.warning(
            "[plan] run=%s | plan REJECTED (%d problem(s)): %s",
            state.get("run_id"), len(errors), "; ".join(errors[:5]),
        )
        state["work_items"] = []          # nothing may act on a rejected plan
        state["workflow_status"] = "needs_human_review"
        for err in errors:
            _note(state, f"[plan] REJECTED - {err}")
    else:
        touched = sum(len(i.target_files) for i in items)
        impacted = sorted({d for deps in impacts.values() for d in deps})
        logger.info(
            "[plan] run=%s | plan accepted: %d item(s), %d file(s), %d dependent file(s)",
            state.get("run_id"), len(items), touched, len(impacted),
        )
        _note(state, f"[plan] {len(items)} item(s) over {touched} file(s); "
                     f"{len(impacted)} other file(s) import them")

    _append_plan_to_report(state, items, errors, impacts)
    return state


def _record_baseline_tests(state: WorkflowState, executor, project_dir: str, inv: dict) -> None:
    """Detect and run the repository's own suite, and record the result as the baseline."""
    command = detect_test_command(executor, project_dir, inv)
    if command is None:
        state["test_command"] = ""
        state["baseline_test"] = {"status": "inconclusive",
                                  "summary": "no test suite was detected in this repository"}
        logger.warning("[acquire] no test suite detected - the change cannot be verified by tests")
        _note(state, "[tests] NONE DETECTED - this repository has no suite this pipeline can run, "
                     "so the change will not be test-verified")
        return

    state["test_command"] = f"{command.label} ({command.evidence})"
    logger.info("[acquire] baseline: running %s (%s) ...", command.label, command.evidence)
    outcome = run_tests(executor, project_dir, command)
    state["baseline_test"] = {"status": outcome.status, "summary": outcome.summary,
                              "label": outcome.label}
    if outcome.status == "inconclusive":
        # Not a failure of anything — it means the suite could not run here (a missing dev
        # dependency, usually). Say so; the alternative is silently gating on a broken baseline.
        _note(state, f"[tests] baseline INCONCLUSIVE - {outcome.summary}; the change cannot be "
                     "verified against this suite")
    else:
        _note(state, f"[tests] baseline: {outcome.summary}")


def change_verify_node(state: WorkflowState) -> WorkflowState:
    """FIXED: re-run the repository's own suite and compare it against the baseline.

    The change gate proves the right FILES changed. This proves the change did not BREAK anything
    the repository could already do — the only evidence of correctness this pipeline can produce
    without understanding the code.

    Only a REGRESSION gates: a check that passed before and fails now. A repo arriving with failing
    tests is common, and treating those as ours would block every change to it. A baseline that
    could not run gates nothing at all, and that is reported rather than quietly treated as green.
    """
    _stage("Change Verify", "re-running the repository's own tests and comparing to the baseline")
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    baseline = state.get("baseline_test") or {}

    if baseline.get("status") not in ("passed", "failed"):
        state["verify_test"] = dict(baseline) or {"status": "inconclusive", "summary": "no baseline"}
        state["verify_verdict"] = "unverified"
        return _note(state, "[verify] SKIPPED - there was no conclusive baseline to compare "
                            "against, so this change is NOT test-verified")

    command = detect_test_command(executor, project_dir, state.get("repo_inventory") or {})
    if command is None:
        state["verify_test"] = {"status": "inconclusive", "summary": "the suite disappeared"}
        state["verify_verdict"] = "unverified"
        return _note(state, "[verify] SKIPPED - no runnable suite after the change")

    outcome = run_tests(executor, project_dir, command)
    state["verify_test"] = {"status": outcome.status, "summary": outcome.summary,
                            "label": outcome.label, "output": outcome.output}

    verdict = classify_regressions(
        baseline={"tests": baseline.get("status") == "passed"},
        current={"tests": outcome.status == "passed"},
    )
    if outcome.status == "inconclusive":
        state["verify_verdict"] = "unverified"
        return _note(state, f"[verify] INCONCLUSIVE - {outcome.summary}; the change is NOT "
                            "test-verified (it was not rejected either)")
    if verdict["regressed"]:
        state["verify_verdict"] = "regressed"
        logger.warning("[verify] REGRESSION: %s", outcome.summary)
        return _note(state, f"[verify] REGRESSION - the suite passed before this change and fails "
                            f"now: {outcome.summary}")
    if verdict["fixed"]:
        state["verify_verdict"] = "fixed"
        return _note(state, f"[verify] the suite was already failing and now passes: {outcome.summary}")
    if outcome.status == "failed":
        state["verify_verdict"] = "preexisting"
        return _note(state, f"[verify] the suite still fails, exactly as it did BEFORE the change "
                            f"({outcome.summary}) - not caused by this change")
    state["verify_verdict"] = "passed"
    return _note(state, f"[verify] the repository's own tests pass: {outcome.summary}")


def code_modifier_node(state: WorkflowState) -> WorkflowState:
    """LLM: implement the current work item against the EXISTING codebase."""
    item = state.get("current_work_item")
    label = getattr(item, "id", "?") if item is not None else "?"
    _stage("Code Modifier", f"implementing work item {label} in the existing codebase")
    return _code_modifier.execute(state)


def change_gate_node(state: WorkflowState) -> WorkflowState:
    """FIXED, deterministic: prove the change was made, and that ONLY the change was made.

    Replaces ``files_complete`` on the brownfield lane, where that check is vacuous — every target
    already exists, so it passes whether or not the editing agent wrote anything, and a run could
    report success with a zero-line diff.

    Writes the same ``gate_result`` shape ``gate_node`` writes, so ``repair_attempt`` accounting and
    the escalate path work over it unchanged.
    """
    _stage("Change Gate", "verifying the targets really changed and nothing else did")
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    item = state.get("current_work_item")

    if item is None:
        state["gate_result"] = {"passed": False, "checks": [
            {"name": "files_changed", "passed": False, "stderr": "no current work item",
             "stdout": "", "exit_code": 1, "scope": ""}]}
        return state

    # Every path the ACCEPTED plan claims — so a file another item legitimately owns is not
    # reported as collateral damage by this item's gate.
    claimed = {
        normalize_target(p)
        for other in (state.get("work_items") or [])
        for p in (other.target_files or [])
    }
    try:
        result = evaluate(
            executor, project_dir,
            action=item.action,
            target_files=list(item.target_files or []),
            baseline_digests=dict(state.get("baseline_digests") or {}),
            claimed_paths=claimed,
        )
    except Exception as exc:  # noqa: BLE001 - an executor blow-up is a gate FAILURE, not a crash
        logger.exception("[change_gate] evaluation failed for item %s", item.id)
        result = {"passed": False, "checks": [
            {"name": "files_changed", "passed": False, "stderr": f"gate error: {exc}",
             "stdout": "", "exit_code": 1, "scope": ""}]}

    state["gate_result"] = result
    failed = [c for c in result["checks"] if not c["passed"]]
    if failed:
        logger.warning("[change_gate] item=%s FAILED: %s", item.id,
                       "; ".join(c["stderr"][:160] for c in failed))
        _note(state, f"[gate] '{item.id}' FAILED: " + "; ".join(c["name"] for c in failed))
    else:
        logger.info("[change_gate] item=%s passed", item.id)
    return state


def change_commit_node(state: WorkflowState) -> WorkflowState:
    """FIXED: commit the change set on the run's own branch, and push it only if asked.

    Deliberately NOT ``publish_sweep``, which greenfield uses: that runs ``git add -A``, which in a
    repository whose ``.gitignore`` we do not control would stage whatever happens to be untracked.
    Here only the files the plan claimed and the gate verified are staged, by explicit path.

    Push is opt-in (``push_enabled``). Without it the run still commits locally, so the diff is
    inspectable, and nothing leaves the machine.
    """
    _stage("Change Commit", "committing the verified change set to the run's branch")
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    branch = (state.get("branch") or "").strip() or "sdlc/change"
    changed = list(state.get("changed_files") or [])

    if not changed:
        state["workflow_status"] = "needs_human_review"
        return _note(state, "[commit] nothing was changed - no commit made")

    rel_paths = [p[len(project_dir) + 1:] if p.startswith(f"{project_dir}/") else p for p in changed]
    cr = state.get("change_request") or {}
    subject = cr.get("title") or cr.get("id") or "apply change request"
    kind = {"bug": "fix", "feature": "feat"}.get(str(cr.get("kind") or ""), "chore")
    message = f"{kind}: {subject}"
    push = bool(state.get("push_enabled")) and bool(state.get("git_remote"))

    try:
        if push and hasattr(executor, "publish_feature"):
            res = executor.publish_feature(
                project_dir, message, rel_paths,
                feature_branch=branch, token=state.get("git_token") or None,
            )
            ok = getattr(res, "exit_code", 1) == 0
            logger.info("[commit] change pushed to '%s' (%s)", branch, "ok" if ok else "PUSH FAILED")
            _note(state, f"[commit] {len(rel_paths)} file(s) pushed to '{branch}'"
                         + ("" if ok else " (PUSH FAILED)"))
        else:
            res = executor.git_commit(project_dir, message)
            ok = bool(getattr(res, "committed", False))
            _note(state, f"[commit] {len(rel_paths)} file(s) committed locally to '{branch}'"
                         + ("" if ok else " (COMMIT FAILED)")
                         + ("" if push else " - push not requested"))
    except Exception as exc:  # noqa: BLE001 - a publish failure must never crash the run
        logger.exception("[commit] failed for run %s", state.get("run_id"))
        return _note(state, f"[commit] FAILED: {exc}")

    state["workflow_status"] = "change_committed"
    return state


def _blast_radius(
    state: WorkflowState, executor, project_dir: str, inv: dict, items: list
) -> dict[str, list[str]]:
    """For each planned target, the files that import it.

    This is the closest thing the pipeline has to impact analysis, and it is REPORTED rather than
    acted on: file-level import edges cannot tell whether a particular edit actually breaks a
    caller, so the honest move is to put the list in front of whoever reviews the pull request
    (see the "KNOWN GAP" note in agents/code_review.py). Never fatal — no graph just means no
    blast-radius section.
    """
    targets = [normalize_target(p) for item in items for p in item.target_files]
    if not targets:
        return {}

    all_source = list(inv.get("source_files") or [])
    source = all_source[:_MAX_GRAPH_FILES]
    contents: dict[str, str] = {}
    for rel in source:
        try:
            contents[rel] = executor.read_file(f"{project_dir}/{rel}")
        except Exception:  # noqa: BLE001 - an unreadable file contributes no edges
            continue
    if not contents:
        return {}

    try:
        graph = build_import_graph(contents)
    except Exception:  # noqa: BLE001 - analysis must never sink the run
        logger.exception("[plan] building the import graph failed")
        return {}

    if len(all_source) > len(source):
        # Say so. An under-built graph makes `dependents_of` return [] for a file that was never
        # scanned, which renders as "nothing imports this" — an affirmative claim about code the
        # analysis never looked at, and the report is the whole deliverable a human reviews.
        logger.warning(
            "[plan] import graph covers %d of %d source files - dependents may be under-reported",
            len(source), len(all_source),
        )
        _note(state, f"[plan] NOTE: blast radius computed over {len(source)} of {len(all_source)} "
                     "source files; dependents may be under-reported")

    # Only report a target the graph actually covers. Omitting an unscanned target lets render_plan
    # print "(not analysed)" instead of an unearned "-".
    return {t: dependents_of(graph, [t]) for t in targets if t in graph}


def _append_plan_to_report(
    state: WorkflowState, items: list, errors: list[str], impacts: dict[str, list[str]]
) -> None:
    """Add the plan section to the change report acquisition already started, and rewrite it."""
    notes = (state.get("change_plan_notes") or "").strip()
    section = render_plan(items, errors, impacts)
    if notes:
        section += "\n## Planner notes\n\n" + notes + "\n"

    report = (state.get("change_report") or "") + "\n" + section
    state["change_report"] = report
    path = state.get("change_report_path")
    if not path:
        return
    try:
        Path(path).write_text(report, encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - a report that cannot be written must not fail the run
        logger.warning("[plan] could not update the change report: %s", exc)


def _write_change_report(state: WorkflowState, inv: RepoInventory) -> None:
    """Persist what acquisition found, in the same reports/<project>-<run>/ folder Code Review and
    Refactoring use — so a brownfield run leaves a readable artifact even when it stops here."""
    cr = state.get("change_request") or {}
    lines = [
        f"# Change Request - {cr.get('title') or cr.get('id') or 'untitled'}",
        "",
        "## Request",
        f"- **ID:** {cr.get('id') or '-'}",
        f"- **Kind:** {cr.get('kind') or '-'}",
        "",
        (cr.get("description") or "_No description supplied._"),
        "",
    ]
    criteria = cr.get("acceptance_criteria") or []
    if criteria:
        lines += ["### Acceptance criteria", ""] + [f"- {c}" for c in criteria] + [""]
    lines += [
        "## Target repository",
        "",
        f"- **Repo:** {state.get('source_repo_url') or '-'}",
        f"- **Base branch:** `{state.get('base_branch') or '-'}`",
        f"- **Base commit:** `{state.get('base_sha') or '-'}`",
        f"- **Working branch:** `{state.get('branch') or '-'}`",
        "",
        "## Inventory",
        "",
        inv.render(),
        "",
    ]
    report = "\n".join(lines)
    state["change_report"] = report

    settings = get_settings()
    folder = Path(settings.reports_dir) / f"{_slug(state.get('project_id') or 'project')}-{_slug(state.get('run_id') or 'run')}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "change-report.md"
        path.write_text(report, encoding="utf-8")
        state["change_report_path"] = str(path)
    except OSError as exc:  # noqa: BLE001 - a report that cannot be written must not fail the run
        logger.warning("[acquire] could not write change report: %s", exc)


def gate_node(state: WorkflowState) -> WorkflowState:
    """FIXED, deterministic quality gate: ``files_complete`` ONLY.

    The gate's sole job is completeness — did the agent write every file this work item was told
    to produce (``target_files``)? It does NOT compile or build the code (that was dropped by
    design: generated source is committed on completeness + human approval, not on a green
    compiler). An executor error (timeout, sandbox/disk failure) is treated as a gate failure —
    recorded as a failing check — rather than crashing the graph. This node is the ROUTER source;
    it makes no routing decision itself.

    A failure here (a missing file) is routed through the repair/escalate path exactly as before:
    repair proposes the missing/fixed file, the gate re-checks. NOTE: ``compile``/``build``/
    ``test``/``lint`` remain on the Executor for later pipeline agents that own them, but are not
    part of this gate.
    """
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    work_item = state.get("current_work_item")
    target_files = work_item.target_files if work_item is not None else []
    checks: list[GateCheck] = []

    try:
        result = executor.files_complete(project_dir, target_files)
        checks.append({"name": result.name, "passed": result.passed, "stderr": result.stderr, "stdout": result.stdout, "exit_code": result.exit_code, "scope": result.scope})
    except Exception as exc:  # noqa: BLE001 - executor failure becomes a gate failure, not a crash
        logger.exception("gate: files_complete raised for run %s", state.get("run_id"))
        checks.append({"name": "files_complete", "passed": False, "stderr": f"executor error: {exc}", "stdout": "", "exit_code": -1, "scope": ""})

    state["gate_result"] = {"passed": bool(checks) and all(c["passed"] for c in checks), "checks": checks}
    return state


def debug_check_node(state: WorkflowState) -> WorkflowState:
    """FIXED, deterministic check for the post-commit Debugging loop: ``compile`` + ``build`` ONLY.

    CLAUDE.md deferred ``compile``/``build`` from the earlier files_complete-only gate to here —
    this is where they finally run. An executor error (timeout, sandbox/disk failure) is treated
    as a failing check — recorded, not raised — rather than crashing the graph (mirrors
    ``gate_node``'s defensive style exactly).
    """
    _stage("Debugging", "compile + build check on the generated code")
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    checks: list[GateCheck] = []

    for name, check in (("compile", executor.compile), ("build", executor.build)):
        try:
            result = check(project_dir)
            checks.append({"name": result.name, "passed": result.passed, "stderr": result.stderr, "stdout": result.stdout, "exit_code": result.exit_code, "scope": result.scope})
        except Exception as exc:  # noqa: BLE001 - executor failure becomes a failing check, not a crash
            logger.exception("debug_check: %s raised for run %s", name, state.get("run_id"))
            checks.append({"name": name, "passed": False, "stderr": f"executor error: {exc}", "stdout": "", "exit_code": -1, "scope": ""})

    state["debug_result"] = {"passed": bool(checks) and all(c["passed"] for c in checks), "checks": checks}
    return state


def unit_test_generate_node(state: WorkflowState) -> WorkflowState:
    """LLM: write unit tests for the generated project, once (no gate/commit here)."""
    _stage("Unit Testing", "generating unit tests for the generated code")
    return _unit_test_agent.execute(state)


def unit_test_run_node(state: WorkflowState) -> WorkflowState:
    """FIXED, deterministic check for the Unit Test phase: ``test`` ONLY.

    A pass here routes on to ``debug_publish`` (persist the loop's fixes + tests to 'dev'), then
    ``documentation`` and ``security`` (the run's actual final stages), which stamp their own
    status; the "completed" set on the passing branch here is an intermediate marker, immediately
    superseded later — kept mainly so a crash between nodes still leaves a meaningful status rather
    than none at all. An executor error is treated as a failing check — recorded, not raised —
    mirroring ``gate_node``/``debug_check_node``. ``workflow_status`` is only set on the passing
    branch, mirroring how ``gate_node`` never sets it at all.
    """
    _stage("Unit Testing", "running the generated test suite")
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"

    try:
        result = executor.test(project_dir)
        check: GateCheck = {"name": result.name, "passed": result.passed, "stderr": result.stderr, "stdout": result.stdout, "exit_code": result.exit_code, "scope": result.scope}
    except Exception as exc:  # noqa: BLE001 - executor failure becomes a failing check, not a crash
        logger.exception("unit_test_run: test raised for run %s", state.get("run_id"))
        check = {"name": "test", "passed": False, "stderr": f"executor error: {exc}", "stdout": "", "exit_code": -1, "scope": ""}

    state["test_result"] = {"passed": check["passed"], "checks": [check]}
    return state


def debug_publish_node(state: WorkflowState) -> WorkflowState:
    """FIXED commit + push of the Debugging<->Unit-Test loop's output to the working branch ('dev').

    The debug/test analogue of ``refactoring_publish_node`` — same shape, same rule-2 boundary
    (the Debugging/Unit-Test AGENTS never form a git call; this deterministic node does the git
    work). Runs on the ``unit_test_run`` pass edge, BEFORE Documentation/Security/finalize, so the
    debug agent's fixes and the generated unit tests land on the remote 'dev' branch that Security
    re-clones and that ``finalize`` opens the ``dev -> main`` PR from. Without this step, those
    files would exist only in the shared sandbox workspace: Security's re-scan (a fresh clone)
    and the PR would carry the pre-debug code and NO tests, so the Testing team would never see
    the tests. ``commit_node`` ran much earlier (right after code generation), so it never captured
    any of this.

    Three shapes, mirroring ``refactoring_publish_node`` / ``feature_publish_node``:
    * Nothing produced by the loop (no unit tests written AND the debug agent never ran) → pass
      through, no commit — keeps the graph acceptance tests' "committed exactly once" invariant on
      runs where the loop was a pure no-op.
    * Push enabled AND the executor supports incremental publish (``publish_sweep``, the local-disk
      executor) → sweep + push everything the loop produced (debug fixes + tests) to the working
      branch in one commit.
    * Otherwise (local-disk ``--no-publish``, or the sandbox/test path) → a plain fixed-path
      ``git_commit`` so the loop's output is at least recorded in the workspace repo; no push is
      available there.

    A publish/commit failure is logged + noted in ``generation_summary`` — never crashes the run
    and never re-enters the debug/test loop (the checks already passed). It deliberately does NOT
    stamp a terminal ``workflow_status`` — Documentation/Security/``package`` (or ``escalate``)
    own the run's true terminal status; this is a mid-pipeline persist step, not the end.

    KNOWN GAP (see also the README "Notes"): ``MCPExecutor`` (the exec-sandbox path —
    ``SANDBOX_ENABLED=true`` / the real ``POST /implementation/start`` API / ``run_fixture.py
    --sandbox``) deliberately has NO ``publish_sweep``/push method: the sandbox's egress is locked
    to PyPI+npm only, with no route to github.com by design (tools/exec-sandbox/squid.conf), so it
    cannot push to a git remote. That path also never sets ``push_enabled``, so this node always
    takes the plain-``git_commit`` branch there — the tests get committed into the sandbox
    container's own local git only, not the pushed GitHub repo. WORKAROUND today: run via the demo
    CLI's default ``--real`` mode (LocalDiskExecutor), which pushes to 'dev' normally so the tests
    reach the remote. A proper fix (host-side "export the finished workspace out of the sandbox,
    then push with real credentials") is deferred — flagged, not built.
    """
    # No-op guard: if the loop produced nothing (no tests generated and the debug agent never ran),
    # there is nothing to persist. Mirrors refactoring_publish's "nothing edited -> skip".
    produced_tests = bool(state.get("unit_tests"))
    # ``debug_rounds``, NOT ``debug_attempt``: the latter is progress-SENSITIVE and resets to 0 on
    # any round that reduced the failure count (see debugging.py), so a debug loop that ran and
    # succeeded reports 0 and this guard would skip publishing the very fixes it just made.
    # ``debug_rounds`` is the monotonic count of rounds actually executed.
    debug_ran = int(state.get("debug_rounds", 0)) > 0
    if not (produced_tests or debug_ran):
        return state

    _stage("Debug Publish", "committing the debug fixes + generated unit tests and pushing 'dev' so "
           "Security's re-scan and the PR carry the tested code and the tests")
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    branch = (state.get("branch") or get_settings().working_branch or "").strip() or "dev"
    message = f"test({state.get('run_id') or 'run'}): debug fixes + unit tests"
    push = bool(state.get("push_enabled")) and bool(state.get("git_remote"))

    try:
        if push and hasattr(executor, "publish_sweep"):
            res = executor.publish_sweep(
                project_dir, feature_branch=branch, token=state.get("git_token") or None
            )
            ok = getattr(res, "exit_code", 1) == 0
            logger.info("[publish] debug/test output pushed to '%s' (%s)", branch, "ok" if ok else "PUSH FAILED")
            state["generation_summary"] = (state.get("generation_summary") or "") + (
                f"[publish] debug fixes + unit tests pushed to '{branch}'" + ("" if ok else " (PUSH FAILED)") + "\n"
            )
        else:
            res = executor.git_commit(project_dir, message)  # LLM never forms/executes this (rule 2)
            ok = bool(getattr(res, "committed", False))
            if not ok:
                logger.warning(
                    "[publish] debug/test local commit FAILED for run %s: %s",
                    state.get("run_id"),
                    (getattr(res, "stderr", "") or getattr(res, "stdout", "")).strip()[:200],
                )
            state["generation_summary"] = (state.get("generation_summary") or "") + (
                f"[publish] {message} committed locally (push not available)"
                + ("" if ok else " (COMMIT FAILED)") + "\n"
            )
    except Exception as exc:  # noqa: BLE001 - a publish failure must never crash the run
        action = "push" if (push and hasattr(executor, "publish_sweep")) else "local commit"
        logger.exception("debug/test %s failed for run %s", action, state.get("run_id"))
        state["generation_summary"] = (state.get("generation_summary") or "") + (
            f"[publish] debug/test {action} FAILED: {exc}\n"
        )
    return state


def documentation_node(state: WorkflowState) -> WorkflowState:
    """Pure LLM: generate project documentation from the final generated source."""
    _stage("Documentation", "writing a README from the final generated source")
    return _documentation_agent.execute(state)


def security_node(state: WorkflowState) -> WorkflowState:
    """Clone the repo into an ephemeral sandbox, run Semgrep, write the security report + verdict.

    The run's actual final analysis stage. Needs ``repo_url``; when absent, writes a report noting
    no repo (same graceful degradation as Code Review) — ``security_verdict`` still gets set (it
    defaults to "approve" when there's nothing to scan), so routing always has a decision to make.
    """
    _stage("Security", f"cloning the repo and running Semgrep (repo_url={state.get('repo_url') or 'none'})")
    return _security_agent.execute(state)


def _change_pr_text(state: WorkflowState) -> tuple[str, str]:
    """Title and body for a brownfield pull request.

    Deliberately NOT the security report greenfield uses. In brownfield that report describes the
    user's OWN pre-existing code, and this PR may be opened on a public repository — publishing a
    vulnerability list about someone else's codebase into a public thread is a disclosure, not a
    courtesy. The body carries the change request, what was edited, and what else imports it.
    """
    cr = state.get("change_request") or {}
    title = str(cr.get("title") or cr.get("id") or "Automated change")
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    changed = [p[len(project_dir) + 1:] if p.startswith(f"{project_dir}/") else p
               for p in (state.get("changed_files") or [])]
    impacts = state.get("change_impacts") or {}

    lines = [
        f"## {title}", "",
        str(cr.get("description") or "").strip() or "_No description supplied._", "",
    ]
    if criteria := cr.get("acceptance_criteria") or []:
        lines += ["### Acceptance criteria", ""] + [f"- {c}" for c in criteria] + [""]

    lines += ["### Files changed", ""] + [f"- `{p}`" for p in changed] + [""]

    impacted = sorted({d for path in changed for d in (impacts.get(path) or [])})
    if impacted:
        lines += [
            "### Other files that import these", "",
            "Not modified by this change — listed so a reviewer can check them:", "",
            *(f"- `{d}`" for d in impacted[:20]),
            "",
        ]
    if notes := (state.get("modifier_notes") or "").strip():
        lines += ["### What the change does", "", notes, ""]

    lines += ["### Verification", "", _verification_line(state), ""]

    lines += [
        "---", "",
        "Opened as a **draft** by an automated change-request run. It has not been merged and "
        "will not be: a human decides whether this is correct.",
        "",
        "This pipeline does not compute a call graph, so it cannot verify that callers outside the "
        "files above still work. Please review accordingly.",
    ]
    return title, "\n".join(lines)[:60000]


def _verification_line(state: WorkflowState) -> str:
    """One honest sentence about what the tests did or did not prove.

    ``unverified`` must never read like a pass. A reviewer skimming a draft PR needs to know
    immediately whether "the tests are green" is a claim being made at all — the most damaging
    thing this report could do is imply verification that never happened.
    """
    verdict = state.get("verify_verdict") or "unverified"
    verify = state.get("verify_test") or {}
    baseline = state.get("baseline_test") or {}
    command = state.get("test_command") or "no suite detected"
    summary = verify.get("summary") or baseline.get("summary") or "no result"

    if verdict == "passed":
        return f"The repository's own tests pass after this change (`{command}`): {summary}."
    if verdict == "fixed":
        return (f"The repository's tests were already failing before this change and now pass "
                f"(`{command}`): {summary}.")
    if verdict == "preexisting":
        return (f"The repository's tests still fail exactly as they did BEFORE this change "
                f"(`{command}`): {summary}. Not caused by this change.")
    if verdict == "regressed":
        return (f"**The tests passed before this change and fail now** (`{command}`): {summary}.")
    return (f"⚠️ **This change is NOT test-verified.** The repository's suite could not be run "
            f"here ({summary}), so nothing was proven either way — please run the tests yourself "
            "before merging.")


def finalize_node(state: WorkflowState) -> WorkflowState:
    """FIXED, deterministic (never LLM-formed): Security approved, so open (or find) the
    `dev -> main` pull request. Never merges — a human approves the merge on GitHub; this keeps a
    shared remote safe. Reached only on ``security_verdict == "approve"`` (see
    ``router.route_after_security``) — a ``changes_requested`` verdict escalates directly instead.
    """
    brownfield = state.get("source_mode") == "brownfield"
    _stage("Finalize", "opening (or finding) the pull request")
    run_id = state.get("run_id") or "-"
    repo_url = (state.get("repo_url") or "").strip()
    head = (state.get("branch") or "dev").strip()

    # A PR needs a branch that exists ON THE REMOTE. Greenfield always pushed (the scaffold created
    # the repo), but brownfield push is opt-in — without it the branch is local only, and asking
    # GitHub to open a PR from it yields a confusing "field head is invalid" rather than the honest
    # "you did not ask me to push anything".
    if brownfield and not (state.get("push_enabled") and state.get("git_remote")):
        state["finalize_status"] = "skipped"
        return _note(state, "[finalize] SKIPPED - the change was committed locally only "
                            "(push was not requested), so there is no remote branch to open a PR from")

    if not repo_url or not is_allowed_repo_url(repo_url):
        logger.info("[finalize] run=%s | no repo_url / not an allowed GitHub URL - skipping PR", run_id)
        state["finalize_status"] = "skipped"
        return _note(state, "[finalize] SKIPPED — no repo_url (nothing was published, so there is "
                            "no branch to open a PR from)")

    match = _OWNER_REPO_RE.match(repo_url)
    if not match:
        logger.warning("[finalize] run=%s | could not parse owner/repo from repo_url: %s", run_id, repo_url)
        state["finalize_status"] = "skipped"
        return _note(state, f"[finalize] SKIPPED — could not parse owner/repo from {repo_url}")
    owner, repo = match.group(1), match.group(2)

    base = (state.get("base_branch") or "main").strip()
    if brownfield:
        title, body = _change_pr_text(state)
    else:
        title = f"Security-approved: merge {head} into {base}"
        body = (state.get("security_report") or "Security scan passed.")[:60000]
    logger.info("[finalize] run=%s | opening %sPR %s -> %s for %s/%s ...",
                run_id, "draft " if brownfield else "", head, base, owner, repo)
    # Pass the credential THIS run pushed with; the client falls back to the configured PAT and
    # then to the `gh` CLI's token, since only the identity that owns the repo can open a PR on it.
    client = get_github_client(token=(state.get("git_token") or "").strip() or None)
    # Brownfield PRs open as DRAFTS: this is a change to a repository the service does not own, and
    # a draft cannot be merged until a person marks it ready — which is the entire safety model,
    # given there is no call-graph analysis to prove the change is safe for callers.
    result = client.create_or_update_pull_request(
        owner, repo, head, base, title, body, draft=brownfield,
    )
    if result.ok:
        state["pr_url"] = result.url
        state["finalize_status"] = "pr_created"
        logger.info("[finalize] run=%s | PR ready: %s", run_id, result.url)
        return _note(state, f"[finalize] PR ready: {result.url}")
    state["finalize_status"] = "pr_failed"
    logger.warning("[finalize] run=%s | PR failed: %s", run_id, result.error)
    # Record it in the run summary too: a PR failure used to be a log line only, so a run could
    # finish "completed" with a zip and nobody noticed the PR never got opened.
    return _note(state, f"[finalize] PR FAILED ({head} -> {base} on {owner}/{repo}): {result.error}")


def package_node(state: WorkflowState) -> WorkflowState:
    """FIXED, deterministic: build the run's downloadable output — a zip of the generated project
    plus its README/review/security reports. Runs after ``finalize`` regardless of whether the PR
    call itself succeeded (a GitHub API hiccup shouldn't withhold the tangible zip output) — but
    only on the approve path (``finalize`` is only reached when Security approved); a
    ``changes_requested`` verdict escalates instead and never reaches packaging.

    Sets the run's true terminal ``workflow_status = "completed"`` — ``unit_test_run_node``'s
    earlier "completed" stamp is just an intermediate marker superseded here.
    """
    _stage("Package", "zipping the generated project + documentation for download")
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    try:
        path = build_project_zip(
            executor=executor,
            project_dir=project_dir,
            generated_code=state.get("generated_code", []),
            unit_tests=state.get("unit_tests", []),
            documentation=state.get("documentation", ""),
            review_report=state.get("review_report", ""),
            security_report=state.get("security_report", ""),
            debugging_report=state.get("debugging_report", ""),
            unit_test_report=state.get("unit_test_report", ""),
        )
    except Exception as exc:  # noqa: BLE001 - a packaging failure must not crash a finished run
        logger.exception("packaging failed for run %s", state.get("run_id"))
        state["generation_summary"] = (state.get("generation_summary") or "") + f"[package] FAILED: {exc}\n"
        state["workflow_status"] = "completed"
        return state
    state["package_path"] = path
    state["workflow_status"] = "completed"
    logger.info("[package] run=%s | zip ready: %s", state.get("run_id") or "-", path)
    return state


def _repo_url_from_remote(remote: str) -> str:
    """A GitHub ``owner/name`` slug -> its https URL; any other remote (URL / local path) is
    returned as-is. Shared by scaffold_node (early push) and commit_node (feature-history push)."""
    remote = (remote or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", remote):
        return f"https://github.com/{remote}"
    return remote


def _feature_commit_message(work_item) -> str:
    """A conventional-commit subject for one work item (its module/feature)."""
    if work_item.screens:
        subject = ", ".join(work_item.screens)
    elif work_item.endpoints:
        subject = ", ".join(work_item.endpoints)
    elif work_item.tables:
        subject = "models " + ", ".join(work_item.tables)
    else:
        subject = f"{len(work_item.target_files)} file(s)"
    return f"feat({work_item.id}): {subject}"


def _item_commit_message(work_item) -> str:
    """Commit subject for one incremental (per-work-item) publish.

    NOTE — deliberate divergence from the batch path's "one commit per user-feature" (rule 6 /
    ``_group_feature_commits``): incremental publish trades that for **one commit per work item**,
    so each item lands on ``dev`` live as it's generated (the whole point of watch-it-fill-in).
    To keep those commits distinguishable when a feature spans several work items, the work-item id
    is included — otherwise every item of a feature would carry an identical ``feat(<feature>): …``
    subject. Items with no feature just use the per-work-item subject.
    """
    if getattr(work_item, "feature_id", None):
        title = work_item.feature_title or work_item.feature_id
        return f"feat({work_item.feature_id}): {title} [{work_item.id}]"
    return _feature_commit_message(work_item)


def _group_feature_commits(work_items) -> list[tuple[str, list[str]]]:
    """Group work items into ONE commit per user-feature (mandatory rule 6).

    Items sharing a ``feature_id`` (assigned by the plan builder from user_features.json /
    user-features.md) collapse into a single ``feat(<feature_id>): <feature_title>`` commit whose
    paths are the union of the group's ``target_files``. Items with no ``feature_id`` are keyed by
    their own id, so they stay one-commit-per-item — exactly the prior behaviour — and their
    message keeps the per-work-item subject. Group order follows first appearance in the plan.
    """
    groups: dict[str, dict] = {}
    order: list[str] = []
    for wi in work_items:
        key = wi.feature_id or wi.id
        if key not in groups:
            groups[key] = {"feature_id": wi.feature_id, "title": wi.feature_title, "items": []}
            order.append(key)
        groups[key]["items"].append(wi)

    commits: list[tuple[str, list[str]]] = []
    for key in order:
        group = groups[key]
        if group["feature_id"]:
            title = group["title"] or group["feature_id"]
            message = f"feat({group['feature_id']}): {title}"
        else:  # ungrouped single item — keep the per-work-item message (legacy behaviour)
            message = _feature_commit_message(group["items"][0])
        paths = list(dict.fromkeys(p for wi in group["items"] for p in wi.target_files))
        commits.append((message, paths))
    return commits


def reconcile_node(state: WorkflowState) -> WorkflowState:
    """FIXED, deterministic post-generation wiring pass (no LLM), run once after the plan is
    exhausted and BEFORE the run-level commit, so the committed repo carries the wired code.

    Each work item was generated in isolation, so cross-file wiring is often left undone — most
    visibly an Express app factory whose module routers are commented out, making no endpoint
    reachable (audit issue 2a). ``app.services.wiring.reconcile_wiring`` repairs what it can
    deterministically over the generated file set and returns only the files it changed; this node
    reads those files through the executor, applies the fixes, and writes them back. Conservative:
    when nothing is safely fixable it writes nothing and the run is byte-identical to before.
    """
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    prefix = f"{project_dir}/"

    # Read the generated source back from the executor keyed by PROJECT-RELATIVE path (the shape the
    # pure reconciler reasons about — relative requires, module dirs). Skip anything unreadable.
    files: dict[str, str] = {}
    for path in state.get("generated_code", []):
        rel = path[len(prefix):] if path.startswith(prefix) else path
        try:
            files[rel] = executor.read_file(path)
        except Exception:  # noqa: BLE001 - a missing/unreadable file just isn't a wiring input
            continue

    try:
        changed = reconcile_wiring(files)
    except Exception as exc:  # noqa: BLE001 - a reconciler bug must never crash a finished plan
        logger.exception("wiring reconciliation failed for run %s", state.get("run_id"))
        state["generation_summary"] = (state.get("generation_summary") or "") + f"[reconcile] FAILED: {exc}\n"
        return state

    # Report-only companion pass: relative imports that resolve to nothing in the generated file
    # set. Deliberately NOT auto-fixed — the two real shapes (a module nobody generated, and one
    # generated under a different convention) are repaired either by creating the file or by
    # renaming the importer, and choosing between them is a judgement call that belongs to the LLM
    # debugging agent. Surfacing them here means that agent is handed the list up front instead of
    # rediscovering them one crash at a time from test stack traces.
    #
    # Scanned over ``{**files, **changed}`` (the wiring fixer's OWN output), not the pre-fix
    # ``files`` — the router fixer above can un-comment a require it just wired in, and analyzing
    # the pre-fix set would miss whatever import that newly-live line introduces.
    try:
        unresolved = find_unresolved_imports({**files, **changed})
    except Exception:  # noqa: BLE001 - an analysis bug must never crash a finished plan
        logger.exception("unresolved-import analysis failed for run %s", state.get("run_id"))
        unresolved = []
    if unresolved:
        logger.warning(
            "[reconcile] run=%s | %d unresolved import(s) — left for the debugging agent",
            state.get("run_id") or "-",
            len(unresolved),
        )
        # Stored as plain dicts (not the dataclass, not pre-rendered strings) so the Debugging
        # agent can both RENDER them (UnresolvedImport.as_note()) and PRUNE them once a later round
        # resolves one — see app.agents.debugging._prune_unresolved. Without a structured form the
        # agent could only ever hand back the identical note text, with no way to tell "still
        # broken" from "fixed two rounds ago".
        state["unresolved_imports"] = [item.to_dict() for item in unresolved]
        state["generation_summary"] = (state.get("generation_summary") or "") + (
            f"[reconcile] {len(unresolved)} unresolved import(s) (not auto-fixed):\n"
            + "".join(f"    - {item.as_note()}\n" for item in unresolved)
        )

    if not changed:
        state["generation_summary"] = (state.get("generation_summary") or "") + "[reconcile] no wiring changes\n"
        return state

    _stage("Reconcile (wiring)", "connecting generated modules (mounting routers) before commit")
    for rel, content in changed.items():
        executor.write_file(prefix + rel, content)
    names = ", ".join(sorted(changed))
    logger.info("[reconcile] run=%s | wired %d file(s): %s", state.get("run_id") or "-", len(changed), names)
    state["generation_summary"] = (state.get("generation_summary") or "") + (
        f"[reconcile] wired {len(changed)} file(s): {names}\n"
    )
    return state


def commit_node(state: WorkflowState) -> WorkflowState:
    """FIXED commit step (never formed by the LLM — CLAUDE.md rule 2). Reached automatically once
    every work item has gate-passed (no human approval — HITL removed).

    Two shapes, chosen by executor capability:
    * If the executor supports ``commit_feature_history`` (the local/real disk executor), produce
      a real branch structure — the scaffold on ``main`` and ONE ``feat(<feature-id>): …`` commit
      per user-feature on ``dev`` (work items sharing a ``feature_id`` collapse into one commit;
      see ``_group_feature_commits``) — so the generated repo carries a per-feature history.
    * Otherwise (the in-memory/sandbox executor), fall back to a single run-level commit, exactly
      as before — keeps the sandbox/test path and its assertions unchanged.
    """
    _stage("Commit / Publish", "finalizing the run - scaffold on 'main', features on 'dev'")
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    work_items = state.get("work_items", [])
    files = state.get("generated_code", [])

    # Incremental live-publish mode: the scaffold ('main') and each feature ('dev') were already
    # committed + pushed live (scaffold_node / feature_publish_node). Here we only sweep up any
    # leftover files and finalize — no re-commit of what already landed on GitHub.
    push = bool(state.get("push_enabled")) and bool(state.get("git_remote"))
    if push and hasattr(executor, "publish_scaffold"):
        note = ""
        sweep_branch = (state.get("branch") or get_settings().working_branch or "").strip() or "dev"
        try:
            res = executor.publish_sweep(
                project_dir, feature_branch=sweep_branch, token=state.get("git_token") or None
            )
            if getattr(res, "exit_code", 0) != 0:
                note = " (sweep push FAILED)"
        except Exception as exc:  # noqa: BLE001 - a sweep failure must not crash the run
            logger.exception("publish sweep failed for run %s", state.get("run_id"))
            note = f" (sweep error: {exc})"
        state["generation_summary"] = (state.get("generation_summary") or "") + (
            f"[commit] live publish complete — scaffold on 'main' + features on 'dev' at "
            f"{state.get('repo_url')}{note}\n"
        )
        state["workflow_status"] = "code_committed"
        return state

    if hasattr(executor, "commit_feature_history"):
        scaffold_files = state.get("scaffold_files", [])
        feature_commits = _group_feature_commits(work_items)  # ONE commit per feature (rule 6)
        # Push (opt-in, mandatory rules 4 & 8): push 'main' after the scaffold and 'dev' after each
        # feature, stopping the run if a push fails. Off unless push_enabled + a remote are set.
        push = bool(state.get("push_enabled")) and bool(state.get("git_remote"))
        try:
            result = executor.commit_feature_history(
                project_dir,
                scaffold_files=scaffold_files,
                feature_commits=feature_commits,
                base_branch="main",
                feature_branch="dev",
                push=push,
                remote=state.get("git_remote") or None,
                token=state.get("git_token") or None,
            )
        except Exception as exc:  # noqa: BLE001 - don't crash the run on a commit failure
            logger.exception("feature-history commit failed for run %s", state.get("run_id"))
            state["generation_summary"] = (state.get("generation_summary") or "") + f"[commit] FAILED: {exc}\n"
            state["workflow_status"] = "commit_failed"  # else the run reports a mid-run status
            return state
        pushed = f" (pushed to '{state.get('git_remote')}')" if push else ""
        if result.exit_code != 0:  # a push failed → run stopped before finishing (rule 8)
            state["generation_summary"] = (state.get("generation_summary") or "") + (
                f"[commit] scaffold on 'main' + feature commit(s) on 'dev' — PUSH FAILED: "
                f"{(result.stderr or result.stdout).strip()[:200]}\n"
            )
            state["workflow_status"] = "push_failed"
            return state
        state["generation_summary"] = (state.get("generation_summary") or "") + (
            f"[commit] scaffold on 'main' + {len(feature_commits)} feature commit(s) on 'dev'{pushed}\n"
        )
        # A successful push makes the repo cloneable — record repo_url so the very next node, Code
        # Review, can clone and analyze it INLINE (the documented contract: repo_url is produced by
        # the push step and consumed by Code Review). A GitHub owner/name slug becomes the https URL;
        # any other remote (URL / local path) is passed through as-is.
        if push:
            state["repo_url"] = _repo_url_from_remote(state.get("git_remote") or "")
        # Not the run's terminal status anymore — Code Review, Refactoring and the debug/test loop
        # run next; Unit Testing sets the actual terminal status.
        state["workflow_status"] = "code_committed"
        return state

    message = f"IMP-001 {state.get('run_id', 'run')}: {len(work_items)} work item(s), {len(files)} file(s)"
    try:
        executor.git_commit(project_dir, message)  # LLM never forms/executes this call (rule 2)
    except Exception as exc:  # noqa: BLE001 - don't crash the run on a commit failure
        logger.exception("commit failed for run %s", state.get("run_id"))
        state["generation_summary"] = (state.get("generation_summary") or "") + f"[commit] FAILED: {exc}\n"
        state["workflow_status"] = "commit_failed"  # else the run reports a mid-run status
        return state
    # Not the run's terminal status anymore — the post-commit Debugging<->Unit-Test loop and then
    # Code Review run next; code_review_node sets the actual terminal status.
    state["workflow_status"] = "code_committed"
    return state


def escalate_node(state: WorkflowState) -> WorkflowState:
    """Terminal failure: a work item hit the repair cap (or codegen never produced valid files).

    Flags ``needs_human_review`` so the orchestrator knows the run needs attention, then ends the
    run. It no longer pauses on an interrupt — that HITL pause had no resume contract and always
    ended the run anyway.
    """
    state["workflow_status"] = "needs_human_review"
    return state
