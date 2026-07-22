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

logger = logging.getLogger(__name__)

_code_generator = CodeGeneratorAgent()
_code_review = CodeReviewAgent()
_unit_test_agent = UnitTestAgent()
_documentation_agent = DocumentationAgent()
_security_agent = SecurityAgent()

# owner/repo out of the same https://github.com/<owner>/<repo> form `is_allowed_repo_url` accepts.
_OWNER_REPO_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")

# Recognized forms of `git_remote` that resolve to a clone-able GitHub HTTPS URL for Code Review -
# a bare "owner/repo" slug (same convention commit_feature_history's own remote-detection regex
# uses), an https://github.com/... URL as-is, or a git@github.com:owner/repo.git SSH remote.
_GITHUB_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_SSH_RE = re.compile(r"^git@github\.com:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$")
_GITHUB_HTTPS_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?$")


def _derive_repo_url(git_remote: str) -> str:
    """Best-effort https://github.com/<owner>/<repo> form of a pushed `git_remote`, for Code
    Review to clone. Any other remote form (a non-GitHub host, a local bare repo path used in
    tests, etc.) returns "" - repo_url stays unset and Code Review keeps its existing graceful
    no-op, rather than guessing at an unrecognized remote."""
    remote = (git_remote or "").strip()
    if _GITHUB_HTTPS_RE.match(remote):
        return remote
    ssh_match = _GITHUB_SSH_RE.match(remote)
    if ssh_match:
        return f"https://github.com/{ssh_match.group(1)}"
    if _GITHUB_SLUG_RE.match(remote):
        return f"https://github.com/{remote}"
    return ""


def scaffold_node(state: WorkflowState) -> WorkflowState:
    """FIXED, deterministic: render the repo-root boilerplate once, before any work item.

    No LLM — Jinja2 templates only (app/services/boilerplate.py). Runs exactly once per run,
    so requirements.txt/package.json exist before the first work item's build check runs. The
    scaffold is INPUT-AWARE: the Design Package's capabilities config decides which files are
    emitted and their contents (absent that config, the legacy FastAPI+React defaults apply).
    """
    logger.info("[scaffold] run=%s | rendering boilerplate...", state.get("run_id") or "-")
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
    logger.info("[scaffold] run=%s | done - %d file(s): %s", state.get("run_id") or "-", len(written), names)
    return state


def code_generator_node(state: WorkflowState) -> WorkflowState:
    """LLM: generate + write files for the current work item (no gate/commit here)."""
    return _code_generator.execute(state)


def code_review_node(state: WorkflowState) -> WorkflowState:
    """Clone the committed repo into an ephemeral sandbox, run static analysis, write the report.

    The agent owns the whole sandbox session (clone → ruff/eslint → sonar-scanner → teardown);
    this node just delegates. Runs right after the run-level commit. Needs ``repo_url`` in state
    to clone; when absent the agent writes a report noting no repo. Stamps ``workflow_status =
    "code_reviewed"`` - an intermediate marker now, not the run's terminal status (Debug/
    Unit-Test/Documentation/Security all run after this).

    NOTE: a Refactoring stage (consuming this node's ``findings.json``) exists on another branch
    and isn't wired in here yet - when it lands, it slots in between this node and ``debug_check``.
    """
    logger.info("[code_review] run=%s | starting (repo_url=%s)", state.get("run_id") or "-", state.get("repo_url") or "none")
    out = _code_review.execute(state)
    logger.info("[code_review] run=%s | done - report at %s", state.get("run_id") or "-", out.get("review_report_path") or "(not saved)")
    return out


def documentation_node(state: WorkflowState) -> WorkflowState:
    """Pure LLM: generate project documentation from the final generated source."""
    logger.info("[documentation] run=%s | starting...", state.get("run_id") or "-")
    out = _documentation_agent.execute(state)
    logger.info("[documentation] run=%s | done - %d char(s) generated", state.get("run_id") or "-", len(out.get("documentation") or ""))
    return out


def security_node(state: WorkflowState) -> WorkflowState:
    """Clone the repo into an ephemeral sandbox, run Semgrep, write the security report.

    Mirrors ``code_review_node`` exactly (own sandbox session, own report), just later in the
    pipeline - the true final stage of the run. Needs ``repo_url``; when absent, writes a report
    noting no repo, same graceful degradation as Code Review.
    """
    logger.info("[security] run=%s | starting (repo_url=%s)", state.get("run_id") or "-", state.get("repo_url") or "none")
    out = _security_agent.execute(state)
    logger.info("[security] run=%s | done - report at %s", state.get("run_id") or "-", out.get("security_report_path") or "(not saved)")
    return out


def finalize_node(state: WorkflowState) -> WorkflowState:
    """FIXED, deterministic (mirrors `commit_node` — never LLM-formed): Security approved, so open
    (or find) the `dev -> main` pull request. Never merges — a human approves the merge on GitHub;
    this keeps a shared remote safe and matches AGENTS_CONTEXT.md §6b ("the Security agent scans,
    it does not merge" — nor does this step auto-merge on its behalf).

    No fix-it loop back to Security: a `changes_requested` verdict routes straight to `escalate`
    (see `router.route_after_security`) rather than to a `refactoring` node. `main` already has its
    own one-shot Refactoring stage (fixes Code Review's findings, between `code_review` and
    `debug_check`) — reusing that name/agent for a second, differently-shaped (looped, security-
    findings-driven) purpose here was a naming and design collision with it, not a real fit.
    """
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
        logger.info("[select] run=%s | work item %d/%d: %s", state.get("run_id") or "-",
                    index + 1, len(items), items[index].id)
    else:
        state["current_work_item"] = None  # plan exhausted -> auto-commit
        logger.info("[select] run=%s | plan exhausted (%d item(s)) -> commit", state.get("run_id") or "-", len(items))
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

    passed = bool(checks) and all(c["passed"] for c in checks)
    state["gate_result"] = {"passed": passed, "checks": checks}
    logger.info("[gate] run=%s | files_complete: %s", state.get("run_id") or "-", "PASS" if passed else "FAIL")
    return state


def debug_check_node(state: WorkflowState) -> WorkflowState:
    """FIXED, deterministic check for the post-commit Debugging loop: ``compile`` + ``build`` ONLY.

    CLAUDE.md deferred ``compile``/``build`` from the earlier files_complete-only gate to here —
    this is where they finally run. An executor error (timeout, sandbox/disk failure) is treated
    as a failing check — recorded, not raised — rather than crashing the graph (mirrors
    ``gate_node``'s defensive style exactly).
    """
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

    passed = bool(checks) and all(c["passed"] for c in checks)
    state["debug_result"] = {"passed": passed, "checks": checks}
    logger.info("[debug_check] run=%s | compile+build: %s", state.get("run_id") or "-", "PASS" if passed else "FAIL")
    return state


def unit_test_generate_node(state: WorkflowState) -> WorkflowState:
    """LLM: write unit tests for the generated project, once (no gate/commit here)."""
    logger.info("[unit_test_generate] run=%s | starting...", state.get("run_id") or "-")
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
    logger.info("[unit_test_run] run=%s | test suite: %s", state.get("run_id") or "-", "PASS" if check["passed"] else "FAIL")
    return state


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
    logger.info("[commit] run=%s | committing generated code...", state.get("run_id") or "-")
    executor = get_executor()
    project_dir = state.get("project_id") or state.get("run_id") or "project"
    work_items = state.get("work_items", [])
    files = state.get("generated_code", [])

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
        if push:
            # A successful push means the code now lives on a real, clone-able 'dev' branch -
            # set repo_url so Code Review (and later, Security) can actually clone and analyze
            # it, instead of silently no-op'ing for lack of a repo URL.
            repo_url = _derive_repo_url(state.get("git_remote") or "")
            if repo_url:
                state["repo_url"] = repo_url
                state["branch"] = "dev"
        state["generation_summary"] = (state.get("generation_summary") or "") + (
            f"[commit] scaffold on 'main' + {len(feature_commits)} feature commit(s) on 'dev'{pushed}\n"
        )
        logger.info("[commit] run=%s | done - %d feature commit(s)%s", state.get("run_id") or "-",
                    len(feature_commits), pushed)
        # Not the run's terminal status anymore — Code Review, Refactoring, the post-commit
        # Debugging<->Unit-Test loop, Documentation, and Security all run next; security_node
        # sets the actual terminal status.
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
    logger.info("[commit] run=%s | done - single run-level commit", state.get("run_id") or "-")
    # Not the run's terminal status anymore — Code Review, Refactoring, the post-commit
    # Debugging<->Unit-Test loop, Documentation, and Security all run next; security_node sets
    # the actual terminal status.
    state["workflow_status"] = "code_committed"
    return state


def escalate_node(state: WorkflowState) -> WorkflowState:
    """Terminal failure: a work item hit the repair cap (or codegen never produced valid files).

    Flags ``needs_human_review`` so the orchestrator knows the run needs attention, then ends the
    run. It no longer pauses on an interrupt — that HITL pause had no resume contract and always
    ended the run anyway.
    """
    logger.warning("[escalate] run=%s | ESCALATED -> needs_human_review", state.get("run_id") or "-")
    state["workflow_status"] = "needs_human_review"
    return state
