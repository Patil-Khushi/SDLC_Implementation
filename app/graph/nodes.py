"""LangGraph node functions.

Each node wraps one step of the IMP-001 subgraph. Agents are instantiated once at import and
reused. The executor is resolved at run time via the provider (``get_executor``), so the same
node code works with the real MCP sandbox (set in the app lifespan) or a FakeExecutor (set in
tests).
"""

from __future__ import annotations

import logging

from app.agents.code_generator import CodeGeneratorAgent
from app.graph.state import GateCheck, WorkflowState
from app.integrations.executor import get_executor
from app.services.boilerplate import render_scaffold

logger = logging.getLogger(__name__)

_code_generator = CodeGeneratorAgent()


def scaffold_node(state: WorkflowState) -> WorkflowState:
    """FIXED, deterministic: render the repo-root boilerplate once, before any work item.

    No LLM — Jinja2 templates only (app/services/boilerplate.py). Runs exactly once per run,
    so requirements.txt/package.json exist before the first work item's build check runs. The
    scaffold is INPUT-AWARE: the Design Package's capabilities config decides which files are
    emitted and their contents (absent that config, the legacy FastAPI+React defaults apply).
    """
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
    return state


def code_generator_node(state: WorkflowState) -> WorkflowState:
    """LLM: generate + write files for the current work item (no gate/commit here)."""
    return _code_generator.execute(state)


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
        state["generation_summary"] = (state.get("generation_summary") or "") + (
            f"[commit] scaffold on 'main' + {len(feature_commits)} feature commit(s) on 'dev'{pushed}\n"
        )
        state["workflow_status"] = "completed"
        return state

    message = f"IMP-001 {state.get('run_id', 'run')}: {len(work_items)} work item(s), {len(files)} file(s)"
    try:
        executor.git_commit(project_dir, message)  # LLM never forms/executes this call (rule 2)
    except Exception as exc:  # noqa: BLE001 - don't crash the run on a commit failure
        logger.exception("commit failed for run %s", state.get("run_id"))
        state["generation_summary"] = (state.get("generation_summary") or "") + f"[commit] FAILED: {exc}\n"
        state["workflow_status"] = "commit_failed"  # else the run reports a mid-run status
        return state
    state["workflow_status"] = "completed"
    return state


def escalate_node(state: WorkflowState) -> WorkflowState:
    """Terminal failure: a work item hit the repair cap (or codegen never produced valid files).

    Flags ``needs_human_review`` so the orchestrator knows the run needs attention, then ends the
    run. It no longer pauses on an interrupt — that HITL pause had no resume contract and always
    ended the run anyway.
    """
    state["workflow_status"] = "needs_human_review"
    return state
