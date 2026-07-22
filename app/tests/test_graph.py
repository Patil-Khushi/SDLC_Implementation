"""Acceptance tests for the IMP-001 subgraph.

Drives the compiled graph with a FakeExecutor (scripted gate outcomes) and a stubbed LLM
gateway (canned codegen + repair replies) — no Docker, no real model. The executor is injected
via set_executor; the module-singleton nodes use the gateway singleton, which we monkeypatch.

Human-in-the-loop was removed: a completed plan auto-commits (no batch-review approval), and a
repair-cap failure ends the run flagged ``needs_human_review`` (no interrupt/pause).
"""

import json
from pathlib import Path

import pytest

import app.agents.security as security_module
import app.graph.nodes as nodes_module
from app.graph.graph import workflow
from app.graph.router import REPAIR_CAP, SECURITY_LOOP_CAP
from app.graph.state import new_state
from app.integrations.executor import FakeExecutor, set_executor
from app.integrations.github import FakeGitHubClient
from app.integrations.review_sandbox import FakeReviewSandbox
from app.models import WorkItem
from app.services import llm_gateway

LOGIN_ITEM = WorkItem(
    id="WI-001",
    requirement_ids=["REQ-1"],
    endpoints=["POST /login"],
    tables=["users"],
    target_files=["app/api/login.py"],
)
# A two-file item where the first codegen reply is INCOMPLETE (only login.py) — used to trip the
# completeness gate (session.py missing) and exercise one repair.
TWO_FILE_ITEM = WorkItem(
    id="WI-010",
    requirement_ids=["REQ-1"],
    endpoints=["POST /login"],
    target_files=["app/api/login.py", "app/api/session.py"],
)
# An item whose second target is NEVER produced by codegen OR repair — trips the gate every time.
NEVER_ITEM = WorkItem(
    id="WI-020",
    requirement_ids=["REQ-1"],
    target_files=["app/api/login.py", "app/api/never.py"],
)

# The gate is completeness-only (no compile). Codegen always writes login.py; a partial reply for
# a multi-file item therefore leaves the gate failing until repair supplies the rest.
CODEGEN_JSON = json.dumps({"files": [{"path": "app/api/login.py", "content": "# v1\n"}], "notes": ""})
# Repair is shown files by their real (project-prefixed) paths and echoes them back; it supplies
# login.py (fixed) + session.py, but never never.py.
REPAIR_JSON = json.dumps(
    {
        "files": [
            {"path": "p1/app/api/login.py", "content": "# v2 fixed\n"},
            {"path": "p1/app/api/session.py", "content": "# session\n"},
        ],
        "notes": "fixed",
    }
)

# The scaffold's 7 rendered boilerplate files (app/services/boilerplate.py) land before any
# work-item file, on every run.
SCAFFOLD_FILE_COUNT = 7


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch):
    # codegen uses complete(); repair uses complete_with_tools() — stub both on the singleton.
    monkeypatch.setattr(llm_gateway.llm_gateway, "complete", lambda *a, **k: CODEGEN_JSON)
    monkeypatch.setattr(llm_gateway.llm_gateway, "complete_with_tools", lambda *a, **k: REPAIR_JSON)
    yield
    set_executor(None)


def _invoke(executor: FakeExecutor, work_items: list[WorkItem], thread_id: str, *, repo_url: str = "") -> dict:
    """Fresh invoke; runs to completion (no HITL pause) and returns the final state."""
    set_executor(executor)
    initial = new_state(run_id="run-1", attempt=7, project_id="p1")
    initial["work_items"] = work_items
    if repo_url:
        initial["repo_url"] = repo_url
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
    workflow.invoke(initial, config)
    return dict(workflow.get_state(config).values)


def test_incomplete_then_completed_repairs_once_then_auto_commits() -> None:
    # Codegen writes only login.py; the gate fails completeness (session.py missing); repair
    # supplies session.py; gate then passes; the run auto-commits (no approval).
    executor = FakeExecutor()
    final = _invoke(executor, [TWO_FILE_ITEM], "t-happy")

    assert final["repair_attempt"] == 1                  # exactly one repair
    assert final["workflow_status"] == "completed"       # gate-passed -> commit -> review -> refactoring -> debug/test (terminal)
    assert len(executor.commits) == 1                    # committed exactly once, run-level
    assert executor.commits[0][0] == "p1"
    assert final["attempt"] == 7                         # orchestrator's counter echoed unchanged
    # the repair supplied the missing file
    assert executor.files["p1/app/api/session.py"] == "# session\n"


def test_never_completing_item_stops_at_cap_needs_human_review_no_commit() -> None:
    # never.py is never produced by codegen or repair → the completeness gate fails every pass.
    executor = FakeExecutor()
    final = _invoke(executor, [NEVER_ITEM], "t-cap")

    assert final["workflow_status"] == "needs_human_review"
    assert final["repair_attempt"] == 3                   # == REPAIR_CAP
    assert final["gate_result"]["checks"][0]["name"] == "files_complete"
    assert "never.py" in final["gate_result"]["checks"][0]["stderr"]
    assert executor.commits == []                         # NO commit on the escalation path


def test_bad_codegen_escalates_without_reaching_gate(monkeypatch) -> None:
    # A generation that never yields valid JSON must NOT reach the gate or produce a commit.
    monkeypatch.setattr(llm_gateway.llm_gateway, "complete", lambda *a, **k: "not json at all")
    executor = FakeExecutor()
    final = _invoke(executor, [LOGIN_ITEM], "t-badcodegen")

    login_files = [f for f in final["generated_code"] if f.endswith("login.py")]
    assert login_files == []                              # nothing written for the work item
    assert final["workflow_status"] == "needs_human_review"
    assert executor.commits == []                          # no commit


def test_missing_target_file_fails_the_completeness_gate(monkeypatch) -> None:
    # The model only ever returns ONE of the two required target files.
    partial_json = json.dumps({"files": [{"path": "app/api/x.py", "content": "# only one\n"}], "notes": ""})
    monkeypatch.setattr(llm_gateway.llm_gateway, "complete", lambda *a, **k: partial_json)
    item = WorkItem(
        id="WI-002",
        requirement_ids=["REQ-2"],
        endpoints=["POST /x"],
        target_files=["app/api/x.py", "app/api/x_missing.py"],
    )
    executor = FakeExecutor()
    final = _invoke(executor, [item], "t-missing")

    checks = final["gate_result"]["checks"]
    assert len(checks) == 1                               # the gate runs files_complete and nothing else
    assert checks[0]["name"] == "files_complete"
    assert checks[0]["passed"] is False
    assert "x_missing.py" in checks[0]["stderr"]
    assert final["repair_attempt"] == REPAIR_CAP           # repair can't conjure the missing file
    assert final["workflow_status"] == "needs_human_review"
    assert executor.commits == []


def test_scaffold_renders_boilerplate_once_before_any_work_item() -> None:
    executor = FakeExecutor()
    final = _invoke(executor, [LOGIN_ITEM], "t-scaffold")

    assert final["workflow_status"] == "completed"        # single item passed -> commit -> review -> refactoring -> debug/test (terminal)
    scaffold_files = [f for f in final["generated_code"] if not f.endswith("login.py")]
    assert len(scaffold_files) == SCAFFOLD_FILE_COUNT
    assert final["generated_code"][0] == "p1/Dockerfile"      # scaffold wrote first, in template order
    assert f"[scaffold] rendered {SCAFFOLD_FILE_COUNT} boilerplate file(s)" in final["generation_summary"]
    # scaffold logs, then the per-item plan, then the item's own outcome — in that order
    summary = final["generation_summary"]
    assert summary.index("[scaffold]") < summary.index("[plan]") < summary.index("[code_generator]")


def test_documentation_and_security_run_after_code_review_on_the_happy_path() -> None:
    # No repo_url is set (push disabled), so Code Review and Security both take their graceful
    # "no repository" no-op path - but Documentation, Security, finalize, and package still ALL
    # run, and the run's true terminal status ("completed") is set by package, not unit_test_run.
    executor = FakeExecutor()
    final = _invoke(executor, [LOGIN_ITEM], "t-full-pipeline")

    assert final["workflow_status"] == "completed"
    assert final["documentation"]  # Documentation ran and produced something (the stubbed LLM reply)
    assert "No repository URL" in final["security_report"]
    assert final["security_report_path"]
    assert final["security_verdict"] == "approve"       # nothing to scan -> defaults to approve
    assert final["finalize_status"] == "skipped"         # no repo_url -> finalize skips the PR
    assert "pr_url" not in final
    assert final["package_path"]                          # the zip was still built
    assert Path(final["package_path"]).exists()


def test_security_approve_opens_pr_and_builds_package(monkeypatch) -> None:
    # A real, allowed repo_url + a clean Semgrep scan (no findings) -> Security approves ->
    # finalize opens a PR via a FakeGitHubClient -> package zips the project. No Docker/network:
    # Security's sandbox and the GitHub client are both faked for this run only.
    def dispatch_complete(prompt, *, system=None, **kwargs):
        if system and "Security step" in system:
            return json.dumps({"executive_summary": "Clean scan, no issues.", "verdict": "approve"})
        return CODEGEN_JSON

    monkeypatch.setattr(llm_gateway.llm_gateway, "complete", dispatch_complete)
    monkeypatch.setattr(
        security_module, "get_review_sandbox",
        lambda: FakeReviewSandbox(files={"main.py": "x = 1\n"}),  # semgrep finds nothing by default
    )
    fake_github = FakeGitHubClient()
    monkeypatch.setattr(nodes_module, "get_github_client", lambda: fake_github)

    executor = FakeExecutor()
    final = _invoke(executor, [LOGIN_ITEM], "t-approve-finalize", repo_url="https://github.com/acme/generated-app")

    assert final["workflow_status"] == "completed"
    assert final["security_verdict"] == "approve"
    assert final["finalize_status"] == "pr_created"
    assert final["pr_url"] == "https://github.com/acme/generated-app/pull/1000"
    assert fake_github.calls == [
        {"owner": "acme", "repo": "generated-app", "head": "dev", "base": "main",
         "title": "Security-approved: merge dev into main"}
    ]
    assert Path(final["package_path"]).exists()


def test_security_changes_requested_loops_then_escalates_no_pr_no_package() -> None:
    # A disallowed repo_url makes Security take its deterministic "changes_requested" no-clone
    # path on EVERY scan (repo_url never changes) — no Docker/sandbox needed. changes_requested
    # loops security -> refactoring -> security up to SECURITY_LOOP_CAP times (refactoring finds
    # nothing actionable each pass, since there's no real finding — just the disallowed-URL note),
    # then escalates: no PR, no zip.
    executor = FakeExecutor()
    final = _invoke(executor, [LOGIN_ITEM], "t-security-escalate", repo_url="https://evil.com/acme/repo")

    assert final["workflow_status"] == "needs_human_review"
    assert final["security_verdict"] == "changes_requested"
    assert final["security_loop_attempt"] == SECURITY_LOOP_CAP  # looped the full cap before giving up
    assert "finalize_status" not in final
    assert "pr_url" not in final
    assert "package_path" not in final


def test_security_loop_exits_via_finalize_once_a_rescan_approves(monkeypatch) -> None:
    # First scan finds a High-severity issue -> forced changes_requested (regardless of the LLM's
    # own verdict — see security._final_verdict) -> refactoring runs once -> loops back to
    # security; the second scan is clean -> approve -> finalize -> package. Verifies the loop's
    # ROUTING/counter mechanics end-to-end; the actual file edit isn't exercised here since
    # complete_with_tools is a canned stub in this harness, not a real tool-execution loop.
    calls = {"n": 0}
    high_severity_semgrep = json.dumps({"results": [
        {"check_id": "python.lang.security.audit.exec-detected", "path": "main.py",
         "start": {"line": 1}, "extra": {"message": "Found exec() call.", "severity": "ERROR"}},
    ]})

    def sandbox_factory():
        calls["n"] += 1
        if calls["n"] == 1:
            from app.integrations.executor import RunResult
            return FakeReviewSandbox(
                files={"main.py": "exec(x)\n"},
                responses={"semgrep": RunResult(stdout=high_severity_semgrep, stderr="", exit_code=1)},
            )
        return FakeReviewSandbox(files={"main.py": "print(x)\n"})  # clean on the re-scan

    def dispatch_complete(prompt, *, system=None, **kwargs):
        if system and "Security step" in system:
            return json.dumps({"executive_summary": "reviewed", "verdict": "approve"})
        return CODEGEN_JSON

    monkeypatch.setattr(llm_gateway.llm_gateway, "complete", dispatch_complete)
    monkeypatch.setattr(security_module, "get_review_sandbox", sandbox_factory)
    fake_github = FakeGitHubClient()
    monkeypatch.setattr(nodes_module, "get_github_client", lambda: fake_github)

    executor = FakeExecutor()
    final = _invoke(executor, [LOGIN_ITEM], "t-loop-fix", repo_url="https://github.com/acme/generated-app")

    assert final["workflow_status"] == "completed"
    assert final["security_verdict"] == "approve"       # ended clean, on the SECOND scan
    assert final["security_loop_attempt"] == 1           # exactly one refactoring pass
    assert final["finalize_status"] == "pr_created"
    assert final["package_path"]
    assert calls["n"] == 2                                # scanned twice: initial + one re-scan
