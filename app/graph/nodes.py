"""LangGraph node functions.

Each node wraps one step of the IMP-001 subgraph. Agents are instantiated once at import and
reused. The executor is resolved at run time via the provider (``get_executor``), so the same
node code works with the real MCP sandbox (set in the app lifespan) or a FakeExecutor (set in
tests).
"""

from __future__ import annotations

import logging
import re

from app.agents.code_generator import CodeGeneratorAgent
from app.agents.code_review import CodeReviewAgent
from app.agents.documentation import DocumentationAgent
from app.agents.security import SecurityAgent
from app.agents.unit_test import UnitTestAgent
from app.graph.state import GateCheck, WorkflowState
from app.integrations.executor import get_executor
from app.integrations.github import get_github_client
from app.integrations.review_sandbox import is_allowed_repo_url
from app.services.boilerplate import render_scaffold
from app.services.packaging import build_project_zip

logger = logging.getLogger(__name__)

_code_generator = CodeGeneratorAgent()
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
    message = _item_commit_message(work_item)
    try:
        res = executor.publish_feature(
            project_dir, message, list(work_item.target_files), token=state.get("git_token") or None
        )
    except Exception as exc:  # noqa: BLE001 - a publish failure must never crash the run
        logger.exception("feature publish failed for run %s", state.get("run_id"))
        state["generation_summary"] = (state.get("generation_summary") or "") + f"[publish] feature push FAILED: {exc}\n"
        return state
    ok = getattr(res, "exit_code", 1) == 0
    logger.info("[publish] feature pushed to 'dev': %s (%s)", message, "ok" if ok else "PUSH FAILED")
    state["generation_summary"] = (state.get("generation_summary") or "") + (
        f"[publish] {message} pushed to 'dev'" + ("" if ok else " (PUSH FAILED)") + "\n"
    )
    return state


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
        checks.append({"name": result.name, "passed": result.passed, "stderr": result.stderr, "exit_code": result.exit_code})
    except Exception as exc:  # noqa: BLE001 - executor failure becomes a gate failure, not a crash
        logger.exception("gate: files_complete raised for run %s", state.get("run_id"))
        checks.append({"name": "files_complete", "passed": False, "stderr": f"executor error: {exc}", "exit_code": -1})

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
            checks.append({"name": result.name, "passed": result.passed, "stderr": result.stderr, "exit_code": result.exit_code})
        except Exception as exc:  # noqa: BLE001 - executor failure becomes a failing check, not a crash
            logger.exception("debug_check: %s raised for run %s", name, state.get("run_id"))
            checks.append({"name": name, "passed": False, "stderr": f"executor error: {exc}", "exit_code": -1})

    state["debug_result"] = {"passed": bool(checks) and all(c["passed"] for c in checks), "checks": checks}
    return state


def unit_test_generate_node(state: WorkflowState) -> WorkflowState:
    """LLM: write unit tests for the generated project, once (no gate/commit here)."""
    _stage("Unit Testing", "generating unit tests for the generated code")
    return _unit_test_agent.execute(state)


def unit_test_run_node(state: WorkflowState) -> WorkflowState:
    """FIXED, deterministic check for the Unit Test phase: ``test`` ONLY.

    A pass here routes on to ``documentation`` (then ``security``, the run's actual final stage),
    which stamp their own status; the "completed" set on the passing branch here is an
    intermediate marker, immediately superseded later — kept mainly so a crash between nodes
    still leaves a meaningful status rather than none at all. An executor error is treated as a
    failing check — recorded, not raised — mirroring ``gate_node``/``debug_check_node``.
    ``workflow_status`` is only set on the passing branch, mirroring how ``gate_node`` never sets
    it at all.
    """
    _stage("Unit Testing", "running the generated test suite")
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"

    try:
        result = executor.test(project_dir)
        check: GateCheck = {"name": result.name, "passed": result.passed, "stderr": result.stderr, "exit_code": result.exit_code}
    except Exception as exc:  # noqa: BLE001 - executor failure becomes a failing check, not a crash
        logger.exception("unit_test_run: test raised for run %s", state.get("run_id"))
        check = {"name": "test", "passed": False, "stderr": f"executor error: {exc}", "exit_code": -1}

    state["test_result"] = {"passed": check["passed"], "checks": [check]}
    if check["passed"]:
        state["workflow_status"] = "completed"
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


def finalize_node(state: WorkflowState) -> WorkflowState:
    """FIXED, deterministic (never LLM-formed): Security approved, so open (or find) the
    `dev -> main` pull request. Never merges — a human approves the merge on GitHub; this keeps a
    shared remote safe. Reached only on ``security_verdict == "approve"`` (see
    ``router.route_after_security``) — a ``changes_requested`` verdict escalates directly instead.
    """
    _stage("Finalize", "opening (or finding) the dev -> main pull request")
    run_id = state.get("run_id") or "-"
    repo_url = (state.get("repo_url") or "").strip()
    head = (state.get("branch") or "dev").strip()

    if not repo_url or not is_allowed_repo_url(repo_url):
        logger.info("[finalize] run=%s | no repo_url / not an allowed GitHub URL - skipping PR", run_id)
        state["finalize_status"] = "skipped"
        return state

    match = _OWNER_REPO_RE.match(repo_url)
    if not match:
        logger.warning("[finalize] run=%s | could not parse owner/repo from repo_url: %s", run_id, repo_url)
        state["finalize_status"] = "skipped"
        return state
    owner, repo = match.group(1), match.group(2)

    title = f"Security-approved: merge {head} into main"
    body = (state.get("security_report") or "Security scan passed.")[:60000]
    logger.info("[finalize] run=%s | opening PR %s -> main for %s/%s ...", run_id, head, owner, repo)
    result = get_github_client().create_or_update_pull_request(owner, repo, head, "main", title, body)
    if result.ok:
        state["pr_url"] = result.url
        state["finalize_status"] = "pr_created"
        logger.info("[finalize] run=%s | PR ready: %s", run_id, result.url)
    else:
        state["finalize_status"] = "pr_failed"
        logger.warning("[finalize] run=%s | PR failed: %s", run_id, result.error)
    return state


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
            documentation=state.get("documentation", ""),
            review_report=state.get("review_report", ""),
            security_report=state.get("security_report", ""),
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
        try:
            res = executor.publish_sweep(project_dir, token=state.get("git_token") or None)
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
