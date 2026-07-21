"""Acceptance tests for the Code Review agent (deterministic findings + LLM interpretation).

Findings come from Ruff/ESLint/SonarQube via the aggregator (facts), which also deterministically
SUPPRESSES well-documented false-positive patterns (test-file asserts, safe auth constants) and
ROLLS UP repeated suppressed findings into one row per rule. The LLM interprets only what remains
ACTIONABLE (summary, observations, recommendations, verdict) - it never sees or re-litigates
suppressed noise. The agent renders a fixed 8-section report split into 4.1 Actionable / 4.2
Suppressed. Uses FakeReviewSandbox + FakeLLMGateway + an injected SonarQube double.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.agents.code_review import CodeReviewAgent
from app.graph.state import WorkflowState, new_state
from app.integrations.executor import RunResult
from app.integrations.review_sandbox import FakeReviewSandbox
from app.integrations.sonarqube import SonarQubeClient
from app.services.llm_gateway import FakeLLMGateway

REPO = "https://github.com/acme/generated-app"

LLM_JSON = json.dumps({
    "executive_summary": "The service works but has an unused import and a complexity hotspot.",
    "verdict": "changes_requested",
    "engineering_observations": [
        {"area": "order flow", "observation": "No transaction boundary around multi-step writes.",
         "severity": "medium", "confidence": "medium"},
    ],
    "recommendations": [
        {"priority": "high", "action": "Add unit tests for the order service."},
    ],
})
RUFF_JSON = json.dumps(
    [{"code": "F401", "message": "'os' imported but unused", "filename": "main.py", "location": {"row": 1, "column": 1}}]
)


def _sonar_off() -> SonarQubeClient:
    return SonarQubeClient(enabled=False)


def _state() -> WorkflowState:
    state = new_state(run_id="run-1", attempt=2, project_id="p1", design_package={"SKILL.md": "snake_case."})
    state["repo_url"] = REPO
    return state


def test_eight_section_report_with_verified_findings_and_observations() -> None:
    fake = FakeReviewSandbox(
        files={"main.py": "import os\nx = 1\n"},
        responses={"ruff": RunResult(stdout=RUFF_JSON, stderr="", exit_code=1)},
    )
    agent = CodeReviewAgent(sandbox_factory=lambda: fake, llm=FakeLLMGateway([LLM_JSON]), sonarqube=_sonar_off())

    out = agent.execute(_state())
    r = out["review_report"]

    assert fake.cloned == [REPO] and fake.opened and fake.closed
    # all 8 sections present, in order
    for s in ("Section 1: Metadata", "Section 2: Executive Summary", "Section 3: Static Analysis Summary",
              "Section 4: Static Analysis Findings", "4.1 Safe Auto-Fix Findings",
              "4.2 AI-Suggested Refactoring Findings", "4.3 Manual Review Required Findings",
              "4.4 Suppressed Findings", "Section 5: Engineering Observations", "Section 6: Metrics",
              "Section 7: Recommendations", "Section 8: Final Verdict"):
        assert s in r
    # F401 is NOT a suppressible rule -> Safe Auto-Fix bucket, tagged Very High (tool-detected)
    assert "CR-001" in r and "F401" in r and "Very High" in r
    assert "Unused Code" in r
    assert "`import os`" in r                              # EVIDENCE: the offending code line
    assert "Why:" in r and "Fix:" in r                     # F401 has a canned root-cause/fix entry
    assert "DELETE_UNUSED_IMPORT" in r                      # explicit machine-actionable operation
    assert "Known gap" in r                                 # dependency/impact-analysis disclaimer
    # LLM interpretation surfaced
    assert "No transaction boundary" in r                 # engineering observation
    assert "Add unit tests for the order service." in r   # recommendation
    assert "CHANGES REQUESTED" in r
    # persisted: both the .md report AND the normalized findings JSON artifact
    path = Path(out["review_report_path"])
    assert path.exists() and path.read_text(encoding="utf-8") == r
    findings_path = Path(out["review_findings_path"])
    assert findings_path.exists()
    data = json.loads(findings_path.read_text(encoding="utf-8"))
    assert data[0]["id"] == "CR-001"
    assert data[0]["evidence"] == "import os"             # code line captured into the JSON
    assert data[0]["sources"] == ["Ruff"] and data[0]["rule_ids"] == ["F401"]
    assert out["workflow_status"] == "code_reviewed"


def test_reviews_the_working_branch_and_pins_commit(monkeypatch) -> None:
    from app.config.settings import get_settings
    monkeypatch.setattr(get_settings(), "working_branch", "dev")

    fake = FakeReviewSandbox(
        files={"main.py": "x = 1\n"},
        responses={
            "--abbrev-ref": RunResult(stdout="dev\n", stderr="", exit_code=0),
            "rev-parse HEAD": RunResult(stdout="abc123def456\n", stderr="", exit_code=0),
        },
    )
    agent = CodeReviewAgent(sandbox_factory=lambda: fake, llm=FakeLLMGateway([LLM_JSON]), sonarqube=_sonar_off())
    out = agent.execute(_state())      # no explicit branch -> falls back to working_branch "dev"

    assert fake.clone_refs == ["dev"]                    # cloned the working branch
    assert out["branch"] == "dev"
    assert out["commit_sha"] == "abc123def456"           # pinned the reviewed commit
    r = out["review_report"]
    assert "| Branch | dev |" in r
    assert "| Commit | abc123def456 |" in r


def test_falls_back_to_default_branch_when_working_branch_missing(monkeypatch) -> None:
    from app.config.settings import get_settings
    monkeypatch.setattr(get_settings(), "working_branch", "dev")

    # First clone (dev) fails; second clone (default branch) succeeds.
    fake = FakeReviewSandbox(
        files={"main.py": "x = 1\n"},
        clone_result=[
            RunResult(stdout="", stderr="Remote branch dev not found in upstream origin", exit_code=128),
            RunResult(stdout="", stderr="", exit_code=0),
        ],
        responses={
            "--abbrev-ref": RunResult(stdout="main\n", stderr="", exit_code=0),
            "rev-parse HEAD": RunResult(stdout="deadbeef1234\n", stderr="", exit_code=0),
        },
    )
    agent = CodeReviewAgent(sandbox_factory=lambda: fake, llm=FakeLLMGateway([LLM_JSON]), sonarqube=_sonar_off())
    out = agent.execute(_state())

    assert fake.clone_refs == ["dev", None]              # tried dev, then default branch
    assert out["branch"] == "main"                       # recorded the actual default branch
    assert "could not be analyzed" not in out["review_report"]   # it recovered
    assert "| Branch | main |" in out["review_report"]


def test_clean_repo_approves_with_no_verified_findings() -> None:
    clean = json.dumps({"executive_summary": "Clean.", "verdict": "approve",
                        "engineering_observations": [], "recommendations": []})
    fake = FakeReviewSandbox(files={"main.py": "x = 1\n"})   # ruff returns nothing (default RunResult ok, empty stdout)
    agent = CodeReviewAgent(sandbox_factory=lambda: fake, llm=FakeLLMGateway([clean]), sonarqube=_sonar_off())

    r = agent.execute(_state())["review_report"]
    assert "APPROVE" in r
    assert "No actionable findings" in r
    assert "_No recommendations._" in r


def test_high_severity_verified_finding_forces_changes_requested() -> None:
    # LLM says approve, but SonarQube reports a HIGH bug -> verdict must be CHANGES REQUESTED.
    approve = json.dumps({"executive_summary": "Looks ok.", "verdict": "approve",
                          "engineering_observations": [], "recommendations": []})

    def fake_http(url, params, headers, timeout):
        return {"issues": [{"key": "K1", "component": "p1:main.py", "line": 10, "severity": "CRITICAL",
                            "message": "Null dereference.", "rule": "python:S2259", "type": "BUG"}]}

    fake = FakeReviewSandbox(files={"main.py": "x = 1\n"})
    agent = CodeReviewAgent(
        sandbox_factory=lambda: fake, llm=FakeLLMGateway([approve]),
        sonarqube=SonarQubeClient(enabled=True, base_url="http://localhost:9000", project_key="p1", http_get=fake_http),
    )
    r = agent.execute(_state())["review_report"]
    assert "Null dereference." in r
    assert "Bug" in r
    assert "CHANGES REQUESTED" in r                       # overridden by the high-severity verified finding


def test_actionable_findings_json_reaches_the_prompt() -> None:
    gateway = FakeLLMGateway([LLM_JSON])
    fake = FakeReviewSandbox(files={"main.py": "import os\n"},
                            responses={"ruff": RunResult(stdout=RUFF_JSON, stderr="", exit_code=1)})
    agent = CodeReviewAgent(sandbox_factory=lambda: fake, llm=gateway, sonarqube=_sonar_off())
    agent.execute(_state())
    prompt = gateway.calls[0]["prompt"]
    assert "Actionable findings" in prompt and "F401" in prompt   # facts handed to the LLM
    assert "Finding counts" in prompt                             # raw/suppressed/actionable counts
    assert "import os" in prompt                                  # source
    assert "snake_case" in prompt                                # SKILL.md


def test_pytest_asserts_in_tests_are_suppressed_not_actionable() -> None:
    # The exact false-positive class from real-world review: Ruff/Bandit S101 (assert-used)
    # flagging ordinary pytest assertions inside tests/ - must NOT appear as an actionable finding
    # and must NOT drive the verdict to CHANGES REQUESTED.
    ruff_asserts = json.dumps([
        {"code": "S101", "message": "Use of assert detected", "filename": f"tests/test_{i}.py",
         "location": {"row": 5, "column": 1}}
        for i in range(5)
    ])
    clean = json.dumps({"executive_summary": "Clean.", "verdict": "approve",
                        "engineering_observations": [], "recommendations": []})
    fake = FakeReviewSandbox(
        files={f"tests/test_{i}.py": "def test_x():\n    x=1\n    assert x == 1\n" for i in range(5)},
        responses={"ruff": RunResult(stdout=ruff_asserts, stderr="", exit_code=1)},
    )
    agent = CodeReviewAgent(sandbox_factory=lambda: fake, llm=FakeLLMGateway([clean]), sonarqube=_sonar_off())
    out = agent.execute(_state())
    r = out["review_report"]

    assert "No actionable findings" in r                # nothing actionable - all 5 were suppressed
    assert "APPROVE" in r                                # suppressed findings never force CHANGES REQUESTED
    assert "S101" in r and "5" in r                       # rolled up: one row, occurrences=5
    assert "assert" in r.lower() and "test" in r.lower()  # the suppression reason is shown

    findings = json.loads(Path(out["review_findings_path"]).read_text(encoding="utf-8"))
    assert len(findings) == 1                            # 5 raw -> 1 rolled-up suppressed row
    assert findings[0]["status"] == "Suppressed"
    assert findings[0]["occurrences"] == 5
    assert len(findings[0]["additional_locations"]) == 4  # 4 more beyond the representative one


def test_bearer_and_error_code_constants_are_suppressed_as_safe() -> None:
    # "bearer" is an RFC 6750 auth-scheme name, not a secret. INVALID_TOKEN="INVALID_TOKEN" is an
    # error-code constant (value equals its own name), not a secret either. Both are classic
    # Ruff/Bandit S105 false positives and must be suppressed, not reported as High severity.
    ruff_json = json.dumps([
        {"code": "S105", "message": 'Possible hardcoded password assigned to: "token_type"',
         "filename": "auth.py", "location": {"row": 3, "column": 1}},
        {"code": "S105", "message": 'Possible hardcoded password assigned to: "INVALID_TOKEN"',
         "filename": "auth.py", "location": {"row": 8, "column": 1}},
    ])
    clean = json.dumps({"executive_summary": "Clean.", "verdict": "approve",
                        "engineering_observations": [], "recommendations": []})
    fake = FakeReviewSandbox(
        files={"auth.py": 'x = 1\n\ntoken_type = "bearer"\ny = 2\nz = 3\nw = 4\nv = 5\n'
                          'INVALID_TOKEN = "INVALID_TOKEN"\n'},
        responses={"ruff": RunResult(stdout=ruff_json, stderr="", exit_code=1)},
    )
    agent = CodeReviewAgent(sandbox_factory=lambda: fake, llm=FakeLLMGateway([clean]), sonarqube=_sonar_off())
    out = agent.execute(_state())

    assert "No actionable findings" in out["review_report"]
    assert "APPROVE" in out["review_report"]
    findings = json.loads(Path(out["review_findings_path"]).read_text(encoding="utf-8"))
    assert all(f["status"] == "Suppressed" for f in findings)


def test_real_hardcoded_secret_stays_actionable_and_high() -> None:
    # A genuine secret-looking value (not a known-safe constant, not equal to its own name) must
    # NOT be suppressed - it stays actionable, High severity, and forces CHANGES REQUESTED.
    ruff_json = json.dumps([
        {"code": "S105", "message": 'Possible hardcoded password assigned to: "db_password"',
         "filename": "config.py", "location": {"row": 2, "column": 1}},
    ])
    approve = json.dumps({"executive_summary": "Looks fine.", "verdict": "approve",
                          "engineering_observations": [], "recommendations": []})
    fake = FakeReviewSandbox(
        files={"config.py": 'x = 1\ndb_password = "hunter2admin"\n'},
        responses={"ruff": RunResult(stdout=ruff_json, stderr="", exit_code=1)},
    )
    agent = CodeReviewAgent(sandbox_factory=lambda: fake, llm=FakeLLMGateway([approve]), sonarqube=_sonar_off())
    out = agent.execute(_state())
    r = out["review_report"]

    assert "CHANGES REQUESTED" in r                       # NOT suppressed -> overrides the LLM's "approve"
    findings = json.loads(Path(out["review_findings_path"]).read_text(encoding="utf-8"))
    assert findings[0]["status"] == "Open"
    assert findings[0]["severity"] == "High"


def test_evidence_populates_for_every_finding_not_just_first_25_files() -> None:
    # Regression test: previously, evidence was only attached for files that happened to be
    # pre-cached (the first _MAX_FILES=25 files, alphabetically) BEFORE SonarQube's findings were
    # even known. A finding on a file sorted late (e.g. "zzz_late/...") or on a non-source file
    # (Dockerfile) silently got NO evidence. Now every finding is read on demand, after aggregation.
    many_files = {f"pkg/mod_{i:03d}.py": f"line0_{i}\nline1_{i}\n" for i in range(30)}   # 30 > _MAX_FILES(25)
    many_files["zzz_late/deep/file.js"] = "const unused = 1;\nconst used = 2;\n"
    many_files["Dockerfile"] = "FROM python:3.12\nCOPY . .\n"

    def fake_http(url, params, headers, timeout):
        if "measures" in url:
            return {"component": {"measures": []}}
        return {"issues": [
            {"key": "K1", "component": "p1:zzz_late/deep/file.js", "line": 1,
             "severity": "MINOR", "message": "Unused variable.", "rule": "javascript:S1481", "type": "CODE_SMELL"},
            {"key": "K2", "component": "p1:Dockerfile", "line": 2,
             "severity": "MAJOR", "message": "Recursive copy risk.", "rule": "docker:S6470", "type": "CODE_SMELL"},
        ]}

    fake = FakeReviewSandbox(files=many_files)
    agent = CodeReviewAgent(
        sandbox_factory=lambda: fake, llm=FakeLLMGateway([LLM_JSON]),
        sonarqube=SonarQubeClient(enabled=True, base_url="http://localhost:9000", project_key="p1", http_get=fake_http),
    )
    out = agent.execute(_state())
    findings = json.loads(Path(out["review_findings_path"]).read_text(encoding="utf-8"))

    late_file = next(f for f in findings if f["file"] == "zzz_late/deep/file.js")
    assert late_file["evidence"] == "const unused = 1;"        # got evidence despite being file #31

    dockerfile = next(f for f in findings if f["file"] == "Dockerfile")
    assert dockerfile["evidence"] == "COPY . ."                 # non-source file also gets evidence


def test_sonar_measures_are_deterministic_and_appear_in_metrics() -> None:
    # Metrics come straight from SonarQube's measures API - the LLM never computes them.
    def fake_http(url, params, headers, timeout):
        if "measures" in url:
            return {"component": {"measures": [
                {"metric": "ncloc", "value": "2911"},
                {"metric": "coverage", "value": "0.0"},
                {"metric": "duplicated_lines_density", "value": "1.6"},
                {"metric": "sqale_index", "value": "125"},
                {"metric": "code_smells", "value": "149"},
            ]}}
        return {"issues": []}

    fake = FakeReviewSandbox(files={"main.py": "x = 1\n"})
    agent = CodeReviewAgent(
        sandbox_factory=lambda: fake, llm=FakeLLMGateway([LLM_JSON]),
        sonarqube=SonarQubeClient(enabled=True, base_url="http://localhost:9000", project_key="p1", http_get=fake_http),
    )
    r = agent.execute(_state())["review_report"]
    assert "measured by SonarQube" in r
    assert "| Lines of code | 2911 | SonarQube |" in r
    assert "| Test coverage | 0.0% | SonarQube |" in r
    assert "| Technical debt | 2h 5m | SonarQube |" in r      # 125 min rendered human-readable


def test_findings_json_field_schema_matches_new_contract() -> None:
    # Guards the findings.json contract for the (currently stubbed) Refactoring agent: every
    # finding must carry the full enrichment field set, not just the original severity/category.
    fake = FakeReviewSandbox(files={"main.py": "import os\nx = 1\n"},
                            responses={"ruff": RunResult(stdout=RUFF_JSON, stderr="", exit_code=1)})
    agent = CodeReviewAgent(sandbox_factory=lambda: fake, llm=FakeLLMGateway([LLM_JSON]), sonarqube=_sonar_off())
    out = agent.execute(_state())
    findings = json.loads(Path(out["review_findings_path"]).read_text(encoding="utf-8"))

    required = {"bucket", "operation", "auto_fix", "risk_level", "requires_tests", "phase",
                "confidence", "verification", "verification_status", "suppressed_reason_kind"}
    assert required.issubset(findings[0].keys())
    assert findings[0]["bucket"] == "Safe Auto-Fix"
    assert findings[0]["operation"] == "DELETE_UNUSED_IMPORT"
    assert findings[0]["auto_fix"] is True
    assert isinstance(findings[0]["confidence"], float)


def test_missing_repo_url_is_a_clean_noop() -> None:
    def _boom():
        raise AssertionError("sandbox must not be created without a repo_url")
    agent = CodeReviewAgent(sandbox_factory=_boom, llm=FakeLLMGateway([]), sonarqube=_sonar_off())
    r = agent.execute(new_state(run_id="r", attempt=0, project_id="p"))["review_report"]
    assert r.startswith("# Code Review Report")
    assert "No repository URL" in r
    assert "Section 8: Final Verdict" in r


def test_clone_failure_degrades_and_tears_down() -> None:
    fake = FakeReviewSandbox(clone_result=RunResult(stdout="", stderr="repository not found", exit_code=128))
    agent = CodeReviewAgent(sandbox_factory=lambda: fake, llm=FakeLLMGateway([]), sonarqube=_sonar_off())
    r = agent.execute(_state())["review_report"]
    assert "could not be analyzed" in r
    assert "CHANGES REQUESTED" in r
    assert fake.closed


def test_bad_json_falls_back_to_formatted_report() -> None:
    fake = FakeReviewSandbox(files={"main.py": "x = 1\n"})
    agent = CodeReviewAgent(sandbox_factory=lambda: fake,
                           llm=FakeLLMGateway(["not json", "still not json"]), sonarqube=_sonar_off())
    r = agent.execute(_state())["review_report"]
    assert r.startswith("# Code Review Report")
    assert "could not be parsed" in r
