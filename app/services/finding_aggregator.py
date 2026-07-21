"""Finding Aggregator - deterministic normalization, dedup, classification, and rollup.

This is the accuracy backbone of the Code Review phase. It takes the raw findings from the
deterministic tools (Ruff, ESLint, SonarQube) and produces ONE normalized, deduplicated,
severity-corrected, suppression-classified list. It contains NO LLM and NO judgement call that
varies run to run: same inputs -> same output, every time.

Three-stage pipeline (all deterministic):
    normalize + dedup (aggregate)  ->  classify (suppress/severity, needs evidence)  ->  rollup

Terminology matters, and each finding now carries TWO independent axes plus a downstream-facing
classification, not one blended "confidence":

* ``verification`` (text: "Very High" / "Low (auto-suppressed...)") - whether a tool DEFINITELY
  detected this exact pattern. It does NOT mean "this is definitely a real, actionable problem".
  A tool can be 100% certain it found a Python `assert` statement while being completely wrong
  that it's a production security risk (Ruff/Bandit's S101 fires on test-file asserts too, which
  are the correct, standard pytest idiom).
* ``confidence`` (float 0.0-1.0) - a machine-actionable "how safe is it to auto-apply this"
  number, derived deterministically from ``verification`` + cross-tool corroboration + ``auto_fix``
  (never a per-rule magic number, so it can't drift out of sync with the rule table).
* ``bucket`` - what should happen to the finding: "Safe Auto-Fix" (deterministic, no reasoning
  needed), "AI Refactoring" (needs reasoning, conditional auto-fix), "Manual Review" (business
  logic / security - never auto-refactor), or "Suppressed" (auto-filtered false positive).
  ``bucket`` is deliberately INDEPENDENT of ``category`` - a finding's category (what kind of
  issue) must never be used to derive what's safe to do with it (that conflation is exactly how
  a naive prefix-based lookup once miscategorized Ruff's flake8-simplify rules (``SIM102``) as
  ``Security`` - see ``_RUFF_RULE_MAP`` below for the precise, ordered fix).

``classify()`` is what tells "tool detected it" apart from "it's actually actionable", using the
same well-documented false-positive patterns real tools solve with `per-file-ignores`/`nosec`
configs - not an LLM guess. Findings that survive classification unsuppressed are what should be
treated as actionable; suppressed ones are kept (with their reason) for transparency, not
silently dropped.

Common finding schema (dict):
    {id, category, severity, sources[], rule_ids[], file, line, column, message, evidence,
     tool_messages[], verification, confidence, verification_status, bucket, operation, auto_fix,
     risk_level, requires_tests, phase, status, suppressed_reason, suppressed_reason_kind,
     occurrences, additional_locations[]}

``status`` is "Open" (actionable) or "Suppressed" (auto-filtered, likely false positive, with
``suppressed_reason``/``suppressed_reason_kind`` explaining why). ``occurrences``/
``additional_locations`` are populated by ``rollup_suppressed()``, which collapses repeated
suppressed findings sharing the same rule(s) AND the same suppression reason (e.g. 450 pytest
asserts) into one row per pattern instead of listing every instance.
"""

from __future__ import annotations

import re
from typing import Any

# Severity scale, high -> low. ("How bad is it if this is left unfixed" - independent of risk_level,
# which is "how risky is it to auto-apply a fix".)
_SEV_RANK = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1, "Info": 0}
_BUCKET_RANK = {"Safe Auto-Fix": 0, "AI Refactoring": 1, "Manual Review": 2, "Suppressed": -1}
_AUTOFIX_RANK = {True: 0, "conditional": 1, False: 2}
_RISK_RANK = {"Low": 0, "Medium": 1, "High": 2}
_LINE_TOLERANCE = 2

# --- deterministic rule -> (category, severity, bucket, operation, auto_fix, risk_level, ---
# --- requires_tests, phase, [why, impact, fix]) mapping -----------------------------------
#
# ONE table per tool is the single source of truth for every enrichment field a finding carries.
# Order matters: entries are matched top-to-bottom, FIRST match wins. Multi-letter rule families
# (SIM, SLF, PERF, UP, C90) are listed BEFORE the single-letter fallbacks (S, E, W, N, D, B) they'd
# otherwise collide with under naive `startswith` matching - this is the precise fix for a real,
# confirmed bug: rule_id "SIM102".startswith("S") is True, so a bare "S" -> Security fallback
# silently miscategorizes every flake8-simplify finding as a High-severity security issue. Anchoring
# the Bandit pattern to `^S\d` (a digit immediately after S) and ordering SIM/SLF ahead of it closes
# that hole for good, not just for this one rule id.
#
# `phase` is the deterministic execution order a Refactoring agent should apply findings in:
# 1 Formatting, 2 Imports, 3 Unused Code/Simplification, 4 Complexity, 5 Bugs/Naming/Docs, 6 Security.

_RUFF_RULE_MAP: list[dict[str, Any]] = [
    # -- exact, well-known rules (carry a why/impact/fix knowledge-base entry) --
    {"match": re.compile(r"^F401$"), "category": "Unused Code", "severity": "Low",
     "bucket": "Safe Auto-Fix", "operation": "DELETE_UNUSED_IMPORT", "auto_fix": True,
     "risk_level": "Low", "requires_tests": False, "phase": 2,
     "why": "The import is never referenced anywhere in the module.",
     "impact": "Dead code; adds noise and a small, unnecessary import-time cost.",
     "fix": "Remove the unused import."},
    {"match": re.compile(r"^F841$"), "category": "Unused Code", "severity": "Low",
     "bucket": "Safe Auto-Fix", "operation": "DELETE_UNUSED_VARIABLE", "auto_fix": True,
     "risk_level": "Low", "requires_tests": False, "phase": 3,
     "why": "A local variable is assigned but never read.",
     "impact": "Likely leftover/debug code, or a bug where the result was meant to be used.",
     "fix": "Remove the assignment, or use the variable if it was meant to be used."},
    {"match": re.compile(r"^F811$"), "category": "Unused Code", "severity": "Low",
     "bucket": "Safe Auto-Fix", "operation": "DELETE_REDEFINITION", "auto_fix": True,
     "risk_level": "Low", "requires_tests": False, "phase": 3},
    {"match": re.compile(r"^C901$"), "category": "Complexity", "severity": "Medium",
     "bucket": "AI Refactoring", "operation": "EXTRACT_FUNCTION", "auto_fix": "conditional",
     "risk_level": "Medium", "requires_tests": True, "phase": 4,
     "why": "The function's cyclomatic complexity exceeds the configured threshold.",
     "impact": "High-complexity functions are harder to test exhaustively and more bug-prone.",
     "fix": "Extract nested branches into smaller, named helper functions."},
    {"match": re.compile(r"^S101$"), "category": "Security", "severity": "Medium",
     "bucket": "Manual Review", "operation": "MANUAL_REVIEW_REQUIRED", "auto_fix": False,
     "risk_level": "High", "requires_tests": True, "phase": 6,
     "why": "Python's `assert` statements are removed entirely when the interpreter runs with "
            "-O (optimized mode), silently disabling whatever check it performs.",
     "impact": "If this code path ever runs under `-O`, the assertion - and any side effect of "
               "evaluating its condition - is skipped without warning.",
     "fix": "Replace with an explicit `if not condition: raise ...` for checks that must always "
            "run, regardless of interpreter flags."},
    {"match": re.compile(r"^S105$"), "category": "Security", "severity": "High",
     "bucket": "Manual Review", "operation": "MANUAL_REVIEW_REQUIRED", "auto_fix": False,
     "risk_level": "High", "requires_tests": True, "phase": 6,
     "why": "A string literal is assigned to a variable whose name suggests a credential.",
     "impact": "If the value is a REAL secret, it is committed to source control in plaintext.",
     "fix": "Load real secrets from environment variables or a secrets manager - never hard-code them."},
    {"match": re.compile(r"^S106$"), "category": "Security", "severity": "High",
     "bucket": "Manual Review", "operation": "MANUAL_REVIEW_REQUIRED", "auto_fix": False,
     "risk_level": "High", "requires_tests": True, "phase": 6,
     "why": "A string literal is passed as a password/token-like function argument.",
     "impact": "If the value is a REAL secret, it is committed to source control in plaintext.",
     "fix": "Load real secrets from environment variables or a secrets manager - never hard-code them."},
    {"match": re.compile(r"^S608$"), "category": "Security", "severity": "Critical",
     "bucket": "Manual Review", "operation": "MANUAL_REVIEW_REQUIRED", "auto_fix": False,
     "risk_level": "High", "requires_tests": True, "phase": 6},
    {"match": re.compile(r"^S324$"), "category": "Security", "severity": "High",
     "bucket": "Manual Review", "operation": "MANUAL_REVIEW_REQUIRED", "auto_fix": False,
     "risk_level": "High", "requires_tests": True, "phase": 6},
    # -- rule-family prefixes (multi-letter - MUST precede the single-letter fallbacks below) --
    {"match": re.compile(r"^SIM\d"), "category": "Best Practice", "severity": "Low",
     "bucket": "Safe Auto-Fix", "operation": "SIMPLIFY_EXPRESSION", "auto_fix": "conditional",
     "risk_level": "Low", "requires_tests": True, "phase": 3},
    {"match": re.compile(r"^SLF\d"), "category": "Best Practice", "severity": "Low",
     "bucket": "Manual Review", "operation": "MANUAL_REVIEW_REQUIRED", "auto_fix": False,
     "risk_level": "Medium", "requires_tests": True, "phase": 5},
    {"match": re.compile(r"^PERF\d"), "category": "Performance", "severity": "Low",
     "bucket": "AI Refactoring", "operation": "OPTIMIZE_PERFORMANCE", "auto_fix": "conditional",
     "risk_level": "Medium", "requires_tests": True, "phase": 5},
    {"match": re.compile(r"^UP\d"), "category": "Code Style", "severity": "Low",
     "bucket": "Safe Auto-Fix", "operation": "MODERNIZE_SYNTAX", "auto_fix": True,
     "risk_level": "Low", "requires_tests": False, "phase": 1},
    {"match": re.compile(r"^C90\d"), "category": "Complexity", "severity": "Medium",
     "bucket": "AI Refactoring", "operation": "EXTRACT_FUNCTION", "auto_fix": "conditional",
     "risk_level": "Medium", "requires_tests": True, "phase": 4},
    {"match": re.compile(r"^I\d"), "category": "Code Style", "severity": "Low",
     "bucket": "Safe Auto-Fix", "operation": "SORT_IMPORTS", "auto_fix": True,
     "risk_level": "Low", "requires_tests": False, "phase": 2},
    # -- single-letter fallbacks (checked LAST, only after every multi-letter family above) --
    {"match": re.compile(r"^E\d"), "category": "Code Style", "severity": "Low",
     "bucket": "Safe Auto-Fix", "operation": "FORMAT_CODE", "auto_fix": True,
     "risk_level": "Low", "requires_tests": False, "phase": 1},
    {"match": re.compile(r"^W\d"), "category": "Code Style", "severity": "Low",
     "bucket": "Safe Auto-Fix", "operation": "FORMAT_CODE", "auto_fix": True,
     "risk_level": "Low", "requires_tests": False, "phase": 1},
    {"match": re.compile(r"^N\d"), "category": "Naming", "severity": "Low",
     "bucket": "Manual Review", "operation": "RENAME_SYMBOL", "auto_fix": False,
     "risk_level": "Medium", "requires_tests": True, "phase": 5},
    {"match": re.compile(r"^D\d"), "category": "Documentation", "severity": "Low",
     "bucket": "Manual Review", "operation": "MANUAL_REVIEW_REQUIRED", "auto_fix": False,
     "risk_level": "Low", "requires_tests": False, "phase": 5},
    {"match": re.compile(r"^B\d"), "category": "Bug", "severity": "Medium",
     "bucket": "Manual Review", "operation": "MANUAL_REVIEW_REQUIRED", "auto_fix": False,
     "risk_level": "Medium", "requires_tests": True, "phase": 5},
    {"match": re.compile(r"^S\d"), "category": "Security", "severity": "High",
     "bucket": "Manual Review", "operation": "MANUAL_REVIEW_REQUIRED", "auto_fix": False,
     "risk_level": "High", "requires_tests": True, "phase": 6},
]
_RUFF_DEFAULT = {"category": "Best Practice", "severity": "Low", "bucket": "Manual Review",
                  "operation": "MANUAL_REVIEW_REQUIRED", "auto_fix": False, "risk_level": "Medium",
                  "requires_tests": True, "phase": 5}

_ESLINT_RULE_MAP: list[dict[str, Any]] = [
    {"keyword": "unused", "category": "Unused Code", "bucket": "Safe Auto-Fix",
     "operation": "DELETE_UNUSED_VARIABLE", "auto_fix": True, "risk_level": "Low",
     "requires_tests": False, "phase": 3},
    {"keyword": "complexity", "category": "Complexity", "bucket": "AI Refactoring",
     "operation": "EXTRACT_FUNCTION", "auto_fix": "conditional", "risk_level": "Medium",
     "requires_tests": True, "phase": 4},
    {"keyword": "security", "category": "Security", "bucket": "Manual Review",
     "operation": "MANUAL_REVIEW_REQUIRED", "auto_fix": False, "risk_level": "High",
     "requires_tests": True, "phase": 6},
    {"keyword": "no-console", "category": "Best Practice", "bucket": "Safe Auto-Fix",
     "operation": "REMOVE_DEBUG_STATEMENT", "auto_fix": True, "risk_level": "Low",
     "requires_tests": False, "phase": 1},
    {"keyword": "eqeqeq", "category": "Best Practice", "bucket": "Safe Auto-Fix",
     "operation": "USE_STRICT_EQUALITY", "auto_fix": True, "risk_level": "Low",
     "requires_tests": False, "phase": 1},
]
_ESLINT_DEFAULT = {"category": "Code Style", "bucket": "Manual Review",
                    "operation": "MANUAL_REVIEW_REQUIRED", "auto_fix": False, "risk_level": "Medium",
                    "requires_tests": True, "phase": 5}

_SONAR_TYPE_CATEGORY = {
    "BUG": "Bug", "VULNERABILITY": "Security", "SECURITY_HOTSPOT": "Security",
    "CODE_SMELL": "Maintainability",
}


def _resolve_ruff_rule(rule_id: str) -> dict[str, Any]:
    rid = (rule_id or "").strip().upper()
    for entry in _RUFF_RULE_MAP:
        if entry["match"].match(rid):
            return entry
    return _RUFF_DEFAULT


def _resolve_eslint_rule(rule_id: str) -> dict[str, Any]:
    r = (rule_id or "").lower()
    for entry in _ESLINT_RULE_MAP:
        if entry["keyword"] in r:
            return entry
    return _ESLINT_DEFAULT


def _sonar_meta(type_: str, severity: str) -> dict[str, Any]:
    """Sonar's own `type` + `severity` (not its rule_id) drive bucket/operation/etc - Sonar's
    CODE_SMELL type spans everything from trivial style to architecture, so severity is used as
    a secondary key to avoid bucketing every code smell the same way."""
    type_ = (type_ or "").upper()
    if type_ in ("BUG", "VULNERABILITY", "SECURITY_HOTSPOT"):
        return {"bucket": "Manual Review", "operation": "MANUAL_REVIEW_REQUIRED", "auto_fix": False,
                "risk_level": "High", "requires_tests": True, "phase": 6}
    if severity in ("Critical", "High"):
        return {"bucket": "Manual Review", "operation": "MANUAL_REVIEW_REQUIRED", "auto_fix": False,
                "risk_level": "Medium", "requires_tests": True, "phase": 5}
    return {"bucket": "AI Refactoring", "operation": "REDUCE_COMPLEXITY", "auto_fix": "conditional",
            "risk_level": "Low", "requires_tests": True, "phase": 4}


def _title_severity(mapped: str) -> str:
    return {"critical": "Critical", "blocker": "Critical", "high": "High", "major": "High",
            "medium": "Medium", "minor": "Medium", "low": "Low", "info": "Info"}.get(
        (mapped or "").lower(), "Low")


# --- normalization (per tool) ---------------------------------------------------------


def _norm_ruff(f: dict[str, Any]) -> dict[str, Any]:
    rule = str(f.get("rule_id", "")).strip()
    meta = _resolve_ruff_rule(rule)
    return _finding(
        category=meta["category"], severity=meta["severity"], source="Ruff", rule_id=rule,
        file=str(f.get("file", "")), line=_int(f.get("line")), column=_int(f.get("column")),
        message=_concise(meta["category"], rule, str(f.get("message", ""))),
        tool_message=f"{rule} {f.get('message', '')}".strip(), evidence=str(f.get("evidence", "")),
        meta=meta,
    )


def _norm_eslint(f: dict[str, Any]) -> dict[str, Any]:
    rule = str(f.get("rule_id", "")).strip()
    meta = _resolve_eslint_rule(rule)
    sev = "Medium" if int(f.get("severity", 1) or 1) == 2 else "Low"
    return _finding(
        category=meta["category"], severity=sev, source="ESLint", rule_id=rule,
        file=str(f.get("file", "")), line=_int(f.get("line")), column=_int(f.get("column")),
        message=_concise(meta["category"], rule, str(f.get("message", ""))),
        tool_message=f"{rule}: {f.get('message', '')}".strip(": "), evidence=str(f.get("evidence", "")),
        meta=meta,
    )


def _norm_sonar(f: dict[str, Any]) -> dict[str, Any]:
    rule = str(f.get("rule_id", "")).strip()
    type_ = str(f.get("type", "")).upper()
    cat = _SONAR_TYPE_CATEGORY.get(type_, "Maintainability")
    sev = _title_severity(str(f.get("severity", "low")))
    meta = _sonar_meta(type_, sev)
    return _finding(
        category=cat, severity=sev, source="SonarQube",
        rule_id=rule, file=str(f.get("file", "")), line=_int(f.get("line")), column=None,
        message=_concise(cat, rule, str(f.get("message", ""))),
        tool_message=str(f.get("message", "")).strip(), evidence="",
        meta=meta,
    )


def _finding(*, category: str, severity: str, source: str, rule_id: str, file: str,
             line: int | None, column: int | None, message: str, tool_message: str,
             evidence: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "", "category": category, "severity": severity,
        "sources": [source], "rule_ids": [rule_id] if rule_id else [],
        "file": file, "line": line, "column": column,
        "message": message, "evidence": evidence,
        "tool_messages": [tool_message] if tool_message else [],
        "verification": "Very High", "status": "Open",
        "suppressed_reason": "", "suppressed_reason_kind": "",
        "occurrences": 1, "additional_locations": [],
        "bucket": meta["bucket"], "operation": meta["operation"], "auto_fix": meta["auto_fix"],
        "risk_level": meta["risk_level"], "requires_tests": meta["requires_tests"],
        "phase": meta["phase"],
        # Finalized by classify() - depends on the fully-merged finding (sources/evidence/status).
        "confidence": 0.0, "verification_status": "",
    }


# --- stage 1: dedup + merge (unchanged: cross-tool corroboration on nearby lines) ------


def aggregate(ruff: list[dict[str, Any]] | None, eslint: list[dict[str, Any]] | None,
              sonar: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize + dedup findings from the three tools into one list (pre-classification)."""
    normalized: list[dict[str, Any]] = []
    normalized += [_norm_ruff(f) for f in (ruff or []) if isinstance(f, dict)]
    normalized += [_norm_eslint(f) for f in (eslint or []) if isinstance(f, dict)]
    normalized += [_norm_sonar(f) for f in (sonar or []) if isinstance(f, dict)]

    merged: list[dict[str, Any]] = []
    for finding in normalized:
        match = _find_duplicate(merged, finding)
        if match is None:
            merged.append(finding)
        else:
            _merge_into(match, finding)

    merged.sort(key=lambda f: (-_SEV_RANK.get(f["severity"], 0), f["file"], f["line"] or 0))
    for i, f in enumerate(merged, start=1):
        f["id"] = f"CR-{i:03d}"
    return merged


def _find_duplicate(existing: list[dict[str, Any]], f: dict[str, Any]) -> dict[str, Any] | None:
    for e in existing:
        if (e["file"] == f["file"] and e["category"] == f["category"]
                and _lines_close(e["line"], f["line"])):
            return e
    return None


def _lines_close(a: int | None, b: int | None) -> bool:
    if a is None or b is None:
        return a == b
    return abs(a - b) <= _LINE_TOLERANCE


def _merge_into(target: dict[str, Any], other: dict[str, Any]) -> None:
    for src in other["sources"]:
        if src not in target["sources"]:
            target["sources"].append(src)
    for rid in other["rule_ids"]:
        if rid not in target["rule_ids"]:
            target["rule_ids"].append(rid)
    for msg in other["tool_messages"]:
        if msg not in target["tool_messages"]:
            target["tool_messages"].append(msg)
    if _SEV_RANK.get(other["severity"], 0) > _SEV_RANK.get(target["severity"], 0):
        target["severity"] = other["severity"]
    if not target["evidence"] and other["evidence"]:
        target["evidence"] = other["evidence"]
    # A merged finding must never be classified as SAFER than any one of its contributing rules -
    # pick the most conservative bucket/auto_fix/risk_level across all merged rules.
    if _BUCKET_RANK.get(other["bucket"], 0) > _BUCKET_RANK.get(target["bucket"], 0):
        target["bucket"] = other["bucket"]
        target["operation"] = other["operation"]
    if _AUTOFIX_RANK.get(other["auto_fix"], 0) > _AUTOFIX_RANK.get(target["auto_fix"], 0):
        target["auto_fix"] = other["auto_fix"]
    if _RISK_RANK.get(other["risk_level"], 0) > _RISK_RANK.get(target["risk_level"], 0):
        target["risk_level"] = other["risk_level"]
    target["requires_tests"] = target["requires_tests"] or other["requires_tests"]
    target["phase"] = max(target["phase"], other["phase"])


# --- stage 2: classification (suppression + verification/confidence finalization) -----
#
# Deterministic, documented false-positive patterns - the same ones real tools solve via
# `per-file-ignores` / `# nosec` configs. Must run AFTER evidence is filled in (S105/S106
# suppression needs the actual assigned value, which lives in the source line, not the tool's
# own message - e.g. Ruff's S105 message only names the VARIABLE, never the value).

_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?/|test_[^/]+\.py$|[^/]+_test\.py$|conftest\.py$|"
    r"[^/]+\.(test|spec)\.[jt]sx?$|__tests__/)",
    re.IGNORECASE,
)

# Assert-used (Ruff/Bandit S101) is suppressed ONLY in test paths - it is a real, if lower,
# concern in production code (asserts vanish under python -O).
_TEST_ONLY_SUPPRESS_RULES = {"S101"}

# Hardcoded-password/secret rules (Ruff/Bandit S105/S106) are a pure name-based heuristic - the
# tool never inspects whether the VALUE is actually secret.
_HARDCODED_SECRET_RULES = {"S105", "S106"}
_ASSIGN_RE = re.compile(r"[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?\s*[:=]\s*[\"']([^\"']*)[\"']")
_SAFE_SECRET_VALUES = {
    "bearer", "basic", "digest", "negotiate", "hawk", "none", "n/a", "na", "",
}
# A test-path secret is still suppressed even when its VALUE isn't a known-safe constant - test
# fixtures are, by convention, not real credentials. The one carve-out: a variable name signalling
# genuine production intent (e.g. a test file that accidentally hardcodes a real prod secret to
# point at a live system) must still surface, since that IS a real defect, not a fixture.
_PROD_INTENT_RE = re.compile(r"(prod|production|real_|live_)", re.IGNORECASE)


def _is_test_path(file: str) -> bool:
    return bool(_TEST_PATH_RE.search((file or "").replace("\\", "/")))


def _is_safe_secret_value(evidence: str) -> bool:
    """True when the assigned string is a known-safe constant, not a real secret.

    Two patterns: (1) a standard, public protocol constant (e.g. the OAuth/HTTP auth-scheme name
    "bearer", per RFC 6750) - never a secret by definition; (2) the value equals the variable's own
    name (e.g. ``INVALID_TOKEN = "INVALID_TOKEN"``) - the classic signature of an error-code/enum
    constant, not a credential.
    """
    match = _ASSIGN_RE.search(evidence or "")
    if not match:
        return False
    name, value = match.group(1), match.group(2)
    if value.strip().lower() in _SAFE_SECRET_VALUES:
        return True
    norm_name = re.sub(r"[_\-]", "", name.strip().lower())
    norm_value = re.sub(r"[_\-]", "", value.strip().lower())
    return bool(norm_value) and norm_name == norm_value


def _is_test_fixture_secret(file: str, evidence: str) -> bool:
    """A hardcoded-secret-shaped finding inside a test/fixture path - broader than
    ``_is_safe_secret_value`` (which only catches known-safe VALUES anywhere): this catches ANY
    assigned string in a test path, UNLESS the variable name signals genuine production intent.
    Deliberately narrow - only the hardcoded-secret rule family, only in test paths, and callers
    still require every rule_id on the finding to be in the suppressible set, so an unrelated
    rule (e.g. a real SQL-injection finding in `conftest.py`) is never hidden by this path.
    """
    if not _is_test_path(file):
        return False
    match = _ASSIGN_RE.search(evidence or "")
    if not match:
        return False
    name = match.group(1)
    return not _PROD_INTENT_RE.search(name)


def _suppress(f: dict[str, Any], *, kind: str, reason: str) -> None:
    f["status"] = "Suppressed"
    f["suppressed_reason"] = reason
    f["suppressed_reason_kind"] = kind
    f["verification"] = "Low (auto-suppressed: likely false positive)"
    f["bucket"] = "Suppressed"
    f["operation"] = "NONE"
    f["auto_fix"] = False


def _verification_status(f: dict[str, Any]) -> str:
    if f.get("status") == "Suppressed":
        return "Suppressed"
    if len(f.get("sources", [])) > 1:
        return "Verified"
    if f.get("evidence"):
        return "Partially Verified"
    return "Tool Only"


def _numeric_confidence(f: dict[str, Any]) -> float:
    """Deterministic 0.0-1.0 auto-fix confidence, derived from already-computed fields (never a
    per-rule magic number) so it can't drift out of sync with the rule table. A Refactoring agent
    should treat anything below ~0.85 as not safe to auto-apply without human review."""
    if f.get("status") == "Suppressed":
        return 0.0
    base = 0.95 if f.get("verification") == "Very High" else 0.5
    if len(f.get("sources", [])) > 1:
        base = min(base + 0.03, 0.99)
    auto_fix = f.get("auto_fix")
    if auto_fix is False:
        base = min(base, 0.60)
    elif auto_fix == "conditional":
        base = min(base, 0.75)
    return round(base, 2)


def classify(findings: list[dict[str, Any]]) -> None:
    """Apply deterministic suppression rules to every finding, IN PLACE, then finalize the two
    derived fields that need the fully-merged finding (`verification_status`, numeric
    `confidence`). Must run AFTER evidence has been filled in. Suppression only fires when EVERY
    rule attached to a (possibly merged) finding is in the known-suppressible set, so a finding
    that also carries an unrelated, non-suppressible rule is never hidden.
    """
    for f in findings:
        rule_ids = {r.upper() for r in f.get("rule_ids", [])}
        file, evidence = f.get("file", ""), f.get("evidence", "")

        if rule_ids and rule_ids.issubset(_TEST_ONLY_SUPPRESS_RULES) and _is_test_path(file):
            _suppress(f, kind="test_assert", reason=(
                "Ruff S101 (assert-used) flags Python's `assert` statement because it is stripped "
                "when the interpreter runs with -O (optimized mode) - a real concern in production "
                "code. Inside test files, `assert` is the correct, standard mechanism pytest relies on."
            ))
        elif rule_ids and rule_ids.issubset(_HARDCODED_SECRET_RULES) and _is_safe_secret_value(evidence):
            _suppress(f, kind="safe_secret_value", reason=(
                "Ruff S105/S106 flags any string literal assigned to a password/token/secret-"
                "looking variable name, regardless of the actual value. The captured value here is "
                "a known-safe constant (an auth-scheme name or an error-code whose value equals its "
                "own name), not a real secret."
            ))
        elif rule_ids and rule_ids.issubset(_HARDCODED_SECRET_RULES) and _is_test_fixture_secret(file, evidence):
            _suppress(f, kind="test_fixture_secret", reason=(
                "Hardcoded-secret-shaped literal found inside a test/fixture path (tests/, "
                "fixtures/, __tests__/, conftest.py, *.test.*, *.spec.*). Test constants of this "
                "shape are standard test fixtures, not real credentials, unless named with an "
                "explicit production-intent signal (e.g. 'prod_', 'real_', 'live_'), which this is not."
            ))

        f["verification_status"] = _verification_status(f)
        f["confidence"] = _numeric_confidence(f)


# --- stage 3: rollup (collapse repeated suppressed findings) --------------------------


def _sort_key(f: dict[str, Any]) -> tuple[Any, ...]:
    """Actionable findings sort by execution PHASE first (Refactoring should apply Formatting
    before Security, regardless of severity), severity as the tiebreaker. Suppressed findings
    never execute, so phase is meaningless for them - they sort by severity/location only, same
    as before."""
    is_suppressed = f.get("status") == "Suppressed"
    phase = 0 if is_suppressed else f.get("phase", 99)
    return (is_suppressed, phase, -_SEV_RANK.get(f["severity"], 0), f["file"], f["line"] or 0)


def rollup_suppressed(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeated SUPPRESSED findings sharing the same rule(s) AND the same suppression
    reason class into ONE row with an occurrence count (e.g. 450 pytest asserts -> one "S101 - 450
    occurrences" row). Grouping by (rule_ids, suppressed_reason_kind) - not rule_ids alone - so two
    suppressions that happen to share a rule but fire for different reasons (e.g. a known-safe
    OAuth constant vs. a test-fixture secret) never collapse under one, possibly misleading, shared
    reason string. ACTIONABLE findings are never rolled up - each needs its own attention and stays
    individually visible. Re-assigns sequential CR-### ids afterward so the final list is still
    cleanly numbered, and re-sorts by execution phase (see ``_sort_key``).
    """
    actionable = [f for f in findings if f.get("status") != "Suppressed"]
    suppressed = [f for f in findings if f.get("status") == "Suppressed"]

    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    for f in suppressed:
        key = (tuple(sorted(f.get("rule_ids", []))), f.get("suppressed_reason_kind", ""))
        if key not in groups:
            rep = dict(f)
            rep["occurrences"] = 0
            rep["additional_locations"] = []
            groups[key] = rep
            order.append(key)
        rep = groups[key]
        rep["occurrences"] += 1
        if not (rep["file"] == f["file"] and rep["line"] == f["line"]):
            rep["additional_locations"].append({"file": f["file"], "line": f["line"]})

    result = actionable + [groups[k] for k in order]
    result.sort(key=_sort_key)
    for i, f in enumerate(result, start=1):
        f["id"] = f"CR-{i:03d}"
    return result


def rule_explanation(rule_ids: list[str]) -> dict[str, str] | None:
    """Deterministic why/impact/fix for a well-known Ruff rule; None when no canned entry exists
    (the renderer falls back to the tool's own message in that case)."""
    for rid in rule_ids:
        entry = _resolve_ruff_rule(rid)
        if "why" in entry:
            return {"why": entry["why"], "impact": entry["impact"], "fix": entry["fix"]}
    return None


# --- helpers --------------------------------------------------------------------------

_CONCISE_BY_RULE = {
    "F401": "Unused import", "F841": "Unused variable", "F811": "Redefinition of unused name",
}
_CONCISE_BY_CATEGORY = {
    "Unused Code": "Unused code", "Complexity": "High complexity", "Security": "Security issue",
    "Bug": "Potential bug", "Naming": "Naming issue", "Documentation": "Missing documentation",
    "Performance": "Performance issue", "Code Style": "Style issue",
    "Maintainability": "Maintainability issue", "Best Practice": "Best-practice issue",
}


def _concise(category: str, rule_id: str, message: str) -> str:
    if rule_id.upper() in _CONCISE_BY_RULE:
        return _CONCISE_BY_RULE[rule_id.upper()]
    return _CONCISE_BY_CATEGORY.get(category, (message or category).split(".")[0][:60].strip())


def _int(value: Any) -> int | None:
    try:
        n = int(value)
        return n or None
    except (TypeError, ValueError):
        return None


def severity_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = {k: 0 for k in _SEV_RANK}
    for f in findings:
        counts[f.get("severity", "Low")] = counts.get(f.get("severity", "Low"), 0) + 1
    return counts


def category_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["category"]] = counts.get(f["category"], 0) + 1
    return counts


def bucket_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"Safe Auto-Fix": 0, "AI Refactoring": 0, "Manual Review": 0}
    for f in findings:
        b = f.get("bucket", "Manual Review")
        if b in counts:
            counts[b] += 1
    return counts
