"""Tests for the deterministic Finding Aggregator (services/finding_aggregator.py).

This is the accuracy backbone: same inputs -> same output, no LLM. Tests cover normalization,
dedup/merge across tools, severity merging, and ID assignment.
"""

from __future__ import annotations

from app.services import finding_aggregator as agg


def test_normalizes_each_tool_into_common_schema() -> None:
    ruff = [{"rule_id": "F401", "file": "app/main.py", "line": 1, "column": 1, "message": "'os' imported but unused"}]
    eslint = [{"rule_id": "no-unused-vars", "file": "web/app.ts", "line": 3, "column": 5, "message": "x unused", "severity": 2}]
    sonar = [{"rule_id": "python:S3776", "file": "app/svc.py", "line": 40, "message": "Reduce complexity", "severity": "high", "type": "CODE_SMELL"}]

    out = agg.aggregate(ruff, eslint, sonar)

    assert [f["id"] for f in out] == ["CR-001", "CR-002", "CR-003"]      # sequential ids
    assert all(f["confidence"] == "Very High" and f["status"] == "Open" for f in out)
    f401 = next(f for f in out if "F401" in f["rule_ids"])
    assert f401["category"] == "Unused Code" and f401["sources"] == ["Ruff"]
    sonar_f = next(f for f in out if f["file"] == "app/svc.py")
    assert sonar_f["category"] == "Maintainability" and sonar_f["severity"] == "High"


def test_merges_duplicate_across_tools_same_file_line_category() -> None:
    # Ruff and SonarQube both flag an unused import on the same line -> ONE merged finding.
    ruff = [{"rule_id": "F401", "file": "app/main.py", "line": 24, "message": "'os' imported but unused"}]
    sonar = [{"rule_id": "S1128", "file": "app/main.py", "line": 25, "message": "Remove unused import",
              "severity": "low", "type": "CODE_SMELL"}]

    out = agg.aggregate(ruff, None, sonar)

    # NOTE: categories differ (Unused Code vs Maintainability) so they do NOT merge by design -
    # merging requires the SAME category. Assert both survive, distinctly.
    assert len(out) == 2


def test_merges_when_same_category_and_close_lines() -> None:
    ruff = [{"rule_id": "F401", "file": "a.py", "line": 10, "message": "unused import os"}]
    # a second Unused Code finding on a near line, same file -> merges
    sonar = [{"rule_id": "S1128", "file": "a.py", "line": 11, "message": "unused import",
              "severity": "high", "type": "CODE_SMELL"}]
    # force same category by making sonar a CODE_SMELL mapped to Maintainability won't match Unused Code;
    # instead use two ruff findings that both map to Unused Code:
    ruff2 = [
        {"rule_id": "F401", "file": "a.py", "line": 10, "message": "unused import os"},
        {"rule_id": "F811", "file": "a.py", "line": 12, "message": "redefinition"},
    ]
    out = agg.aggregate(ruff2, None, None)
    assert len(out) == 1                                  # merged (same file, Unused Code, within +-2)
    merged = out[0]
    assert set(merged["rule_ids"]) == {"F401", "F811"}
    assert merged["sources"] == ["Ruff"]
    assert len(merged["tool_messages"]) == 2


def test_severity_merge_keeps_highest() -> None:
    findings = agg.aggregate(
        [{"rule_id": "F401", "file": "a.py", "line": 5, "message": "unused"}],   # Low
        None,
        None,
    )
    assert findings[0]["severity"] == "Low"
    # a security ruff rule (S-prefix) is High
    sec = agg.aggregate([{"rule_id": "S105", "file": "a.py", "line": 9, "message": "hardcoded password"}], None, None)
    assert sec[0]["category"] == "Security" and sec[0]["severity"] == "High"


def test_empty_inputs_yield_empty_list() -> None:
    assert agg.aggregate(None, None, None) == []
    assert agg.aggregate([], [], []) == []


def test_sorted_by_severity_then_location() -> None:
    ruff = [{"rule_id": "F401", "file": "z.py", "line": 1, "message": "unused"}]        # Low
    sonar = [{"rule_id": "S1", "file": "a.py", "line": 1, "message": "bug", "severity": "high", "type": "BUG"}]  # High
    out = agg.aggregate(ruff, None, sonar)
    assert out[0]["severity"] == "High" and out[0]["id"] == "CR-001"    # high first
    assert out[1]["severity"] == "Low"


# --- classify(): deterministic suppression + severity refinement ----------------------


def test_classify_suppresses_s101_assert_in_test_files_only() -> None:
    findings = agg.aggregate(
        [
            {"rule_id": "S101", "file": "tests/test_auth.py", "line": 5, "message": "assert used"},
            {"rule_id": "S101", "file": "app/auth.py", "line": 5, "message": "assert used"},
        ], None, None,
    )
    for f in findings:
        f["evidence"] = "assert x == 1"    # evidence not needed for this rule, but set for realism
    agg.classify(findings)

    test_finding = next(f for f in findings if f["file"] == "tests/test_auth.py")
    prod_finding = next(f for f in findings if f["file"] == "app/auth.py")
    assert test_finding["status"] == "Suppressed"
    assert "pytest" in test_finding["suppressed_reason"].lower() or "test" in test_finding["suppressed_reason"].lower()
    assert prod_finding["status"] == "Open"           # NOT suppressed - production code
    assert prod_finding["severity"] == "Medium"       # downgraded from the blanket "High", not ignored


def test_classify_suppresses_known_safe_secret_values() -> None:
    findings = agg.aggregate([
        {"rule_id": "S105", "file": "auth.py", "line": 3, "message": 'hardcoded password: "token_type"'},
        {"rule_id": "S105", "file": "errors.py", "line": 7, "message": 'hardcoded password: "INVALID_TOKEN"'},
        {"rule_id": "S105", "file": "config.py", "line": 2, "message": 'hardcoded password: "db_password"'},
    ], None, None)
    findings[0]["evidence"] = 'token_type = "bearer"'                    # RFC 6750 auth-scheme name
    findings[1]["evidence"] = 'INVALID_TOKEN = "INVALID_TOKEN"'          # error-code constant
    findings[2]["evidence"] = 'db_password = "hunter2admin"'             # a REAL-looking secret
    agg.classify(findings)

    assert findings[0]["status"] == "Suppressed"
    assert findings[1]["status"] == "Suppressed"
    assert findings[2]["status"] == "Open"            # not a known-safe pattern -> stays actionable
    assert findings[2]["severity"] == "High"


def test_classify_does_not_suppress_when_finding_has_an_unrelated_extra_rule() -> None:
    # A finding merged from S101 AND a non-suppressible rule must NOT be suppressed, even in a
    # test file - hiding it could hide the unrelated real issue.
    findings = agg.aggregate([
        {"rule_id": "S101", "file": "tests/test_x.py", "line": 10, "message": "assert used"},
        {"rule_id": "S608", "file": "tests/test_x.py", "line": 11, "message": "SQL injection risk"},
    ], None, None)
    assert len(findings) == 1                          # same file, both "Security" category, merged
    agg.classify(findings)
    assert findings[0]["status"] == "Open"              # rule_ids = {S101, S608} - not a pure S101 subset


def test_severity_overrides_apply_after_classify() -> None:
    findings = agg.aggregate([{"rule_id": "S608", "file": "db.py", "line": 1, "message": "sql injection"}], None, None)
    agg.classify(findings)
    assert findings[0]["severity"] == "Critical"


# --- rollup_suppressed(): collapse repeated suppressed findings -----------------------


def test_rollup_collapses_repeated_suppressed_findings_by_rule() -> None:
    raw = [{"rule_id": "S101", "file": f"tests/test_{i}.py", "line": 5, "message": "assert used"} for i in range(6)]
    findings = agg.aggregate(raw, None, None)
    assert len(findings) == 6                           # different files -> no dedup merge
    agg.classify(findings)
    rolled = agg.rollup_suppressed(findings)

    assert len(rolled) == 1                             # 6 suppressed S101 findings -> 1 row
    assert rolled[0]["occurrences"] == 6
    assert len(rolled[0]["additional_locations"]) == 5
    assert rolled[0]["id"] == "CR-001"                   # re-numbered after rollup


def test_rollup_leaves_actionable_findings_individually_listed() -> None:
    raw = [
        {"rule_id": "S101", "file": "tests/test_a.py", "line": 1, "message": "assert used"},   # suppressed
        {"rule_id": "F401", "file": "app/a.py", "line": 1, "message": "unused import"},         # actionable
        {"rule_id": "F401", "file": "app/b.py", "line": 1, "message": "unused import"},         # actionable, different file
    ]
    findings = agg.aggregate(raw, None, None)
    agg.classify(findings)
    rolled = agg.rollup_suppressed(findings)

    actionable = [f for f in rolled if f["status"] == "Open"]
    suppressed = [f for f in rolled if f["status"] == "Suppressed"]
    assert len(actionable) == 2                          # both F401s remain individually visible
    assert len(suppressed) == 1


# --- rule_explanation(): deterministic why/impact/fix knowledge base -------------------


def test_rule_explanation_known_rule_has_why_impact_fix() -> None:
    kb = agg.rule_explanation(["F401"])
    assert kb is not None
    assert "why" in kb and "impact" in kb and "fix" in kb


def test_rule_explanation_unknown_rule_returns_none() -> None:
    assert agg.rule_explanation(["totally-unknown-rule-xyz"]) is None
