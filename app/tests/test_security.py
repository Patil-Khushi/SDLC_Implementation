"""Security Agent tests - Semgrep findings + LLM interpretation -> a compact report.

Mirrors app/tests/test_code_review.py's FakeReviewSandbox pattern (no real Docker/network).
"""

from __future__ import annotations

import json
from pathlib import Path

from app.agents.security import SecurityAgent
from app.integrations.executor import RunResult
from app.integrations.review_sandbox import FakeReviewSandbox
from app.services.llm_gateway import FakeLLMGateway

REPO = "https://github.com/acme/generated-app"

SEMGREP_JSON = json.dumps({
    "results": [
        {"check_id": "python.lang.security.audit.exec-detected", "path": "main.py",
         "start": {"line": 5}, "extra": {"message": "Found exec() call.", "severity": "ERROR"}},
    ]
})

LLM_JSON = json.dumps({"executive_summary": "One high-severity finding needs attention.", "verdict": "changes_requested"})


def _state(**over) -> dict:
    base = {"run_id": "r1", "project_id": "p1", "repo_url": REPO}
    base.update(over)
    return base


def test_semgrep_findings_parse_and_render() -> None:
    fake = FakeReviewSandbox(
        files={"main.py": "exec(x)\n"},
        responses={"semgrep": RunResult(stdout=SEMGREP_JSON, stderr="", exit_code=1)},
    )
    agent = SecurityAgent(sandbox_factory=lambda: fake, llm=FakeLLMGateway([LLM_JSON]))

    out = agent.execute(_state())
    r = out["security_report"]

    assert fake.cloned == [REPO] and fake.opened and fake.closed
    assert "exec-detected" in r
    assert "High" in r
    assert "CHANGES REQUESTED" in r
    assert "One high-severity finding needs attention." in r

    payload = json.loads(Path(out["security_report_path"].replace("security-report.md", "security-findings.json")).read_text())
    assert payload["verdict"] == "changes_requested"
    assert payload["findings_count"] == 1
    assert payload["summary"] == "One high-severity finding needs attention."
    assert payload["findings"][0]["rule"] == "python.lang.security.audit.exec-detected"
    assert payload["findings"][0]["severity"] == "High"


def test_missing_repo_url_is_a_clean_noop() -> None:
    def _boom():
        raise AssertionError("sandbox must not be created without a repo_url")
    agent = SecurityAgent(sandbox_factory=_boom, llm=FakeLLMGateway([]))
    out = agent.execute(_state(repo_url=""))
    assert "No repository URL" in out["security_report"]


def test_disallowed_repo_url_is_refused_without_cloning() -> None:
    def _boom():
        raise AssertionError("sandbox must not be created for a disallowed repo_url")
    agent = SecurityAgent(sandbox_factory=_boom, llm=FakeLLMGateway([]))
    out = agent.execute(_state(repo_url="https://evil.com/acme/repo"))
    assert "not an allowed GitHub URL" in out["security_report"]
    assert "CHANGES REQUESTED" in out["security_report"]
    # Empty findings here means "refused to scan", NOT "clean scan" - the APPROVED callout
    # (guarded on verdict == approve) must never appear for this error case.
    assert "No security issues found - APPROVED" not in out["security_report"]


def test_clean_repo_approves_with_no_findings() -> None:
    clean = json.dumps({"executive_summary": "No issues found.", "verdict": "approve"})
    fake = FakeReviewSandbox(files={"main.py": "x = 1\n"})  # semgrep returns nothing by default
    agent = SecurityAgent(sandbox_factory=lambda: fake, llm=FakeLLMGateway([clean]))

    out = agent.execute(_state())
    assert "APPROVE" in out["security_report"]
    assert "No security issues found - APPROVED." in out["security_report"]
    assert "_No findings - nothing for Semgrep to report._" in out["security_report"]

    payload = json.loads(Path(out["security_report_path"].replace("security-report.md", "security-findings.json")).read_text())
    assert payload == {"verdict": "approve", "summary": "No issues found.", "findings_count": 0, "findings": []}


def test_report_and_findings_persist_to_disk() -> None:
    fake = FakeReviewSandbox(
        files={"main.py": "exec(x)\n"},
        responses={"semgrep": RunResult(stdout=SEMGREP_JSON, stderr="", exit_code=1)},
    )
    agent = SecurityAgent(sandbox_factory=lambda: fake, llm=FakeLLMGateway([LLM_JSON]))

    out = agent.execute(_state())
    path = Path(out["security_report_path"])
    assert path.exists() and path.read_text(encoding="utf-8") == out["security_report"]
    assert out["workflow_status"] == "security_reviewed"


def test_clone_failure_degrades_gracefully() -> None:
    fake = FakeReviewSandbox(clone_result=RunResult(stdout="", stderr="repository not found", exit_code=128))
    agent = SecurityAgent(sandbox_factory=lambda: fake, llm=FakeLLMGateway([]))
    out = agent.execute(_state())
    assert "could not be scanned" in out["security_report"]
    assert "CHANGES REQUESTED" in out["security_report"]
    assert fake.closed
