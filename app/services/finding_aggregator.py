"""Finding Aggregator - deterministic normalization, dedup, classification, and rollup.

This is the accuracy backbone of the Code Review phase. It takes the raw findings from the
deterministic tools (Ruff, ESLint, SonarQube) and produces ONE normalized, deduplicated,
severity-corrected, suppression-classified list. It contains NO LLM and NO judgement call that
varies run to run: same inputs -> same output, every time.

Three-stage pipeline (all deterministic):
    normalize + dedup (aggregate)  ->  classify (suppress/severity, needs evidence)  ->  rollup

Terminology matters: ``confidence: "Very High"`` means a tool DEFINITELY detected this exact
pattern - it does NOT mean "this is definitely a real, actionable problem". Those are different
axes. A tool can be 100% certain it found a Python `assert` statement while being completely wrong
that it's a production security risk (Ruff/Bandit's S101 fires on test-file asserts too, which are
the correct, standard pytest idiom). ``classify()`` is what tells the two apart, using the SAME
well-documented false-positive patterns real tools solve with `per-file-ignores`/`nosec` configs -
not an LLM guess. Findings that survive classification unsuppressed are what should be treated as
actionable; suppressed ones are kept (with their reason) for transparency, not silently dropped.

Common finding schema (dict):
    {id, category, severity, sources[], rule_ids[], file, line, column, message, evidence,
     tool_messages[], confidence, status, suppressed_reason, occurrences, additional_locations[]}

``status`` is "Open" (actionable) or "Suppressed" (auto-filtered, likely false positive, with
``suppressed_reason`` explaining why). ``occurrences``/``additional_locations`` are populated by
``rollup_suppressed()``, which collapses repeated suppressed findings (e.g. 450 pytest asserts)
into one row per rule instead of listing every instance.
"""

from __future__ import annotations

import re
from typing import Any

# Severity scale, high -> low.
_SEV_RANK = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1, "Info": 0}
_LINE_TOLERANCE = 2

# --- category inference ---------------------------------------------------------------

_RUFF_PREFIX_CATEGORY = {
    "F401": "Unused Code", "F811": "Unused Code", "F841": "Unused Code",
    "E": "Code Style", "W": "Code Style", "I": "Code Style",
    "N": "Naming", "D": "Documentation", "S": "Security",
    "B": "Bug", "C90": "Complexity", "PERF": "Performance",
}
_ESLINT_KEYWORD_CATEGORY = {
    "unused": "Unused Code", "complexity": "Complexity", "security": "Security",
    "no-console": "Best Practice", "eqeqeq": "Best Practice",
}
_SONAR_TYPE_CATEGORY = {
    "BUG": "Bug", "VULNERABILITY": "Security", "SECURITY_HOTSPOT": "Security",
    "CODE_SMELL": "Maintainability",
}


def _ruff_category(rule_id: str) -> str:
    rule_id = (rule_id or "").upper()
    for prefix, cat in _RUFF_PREFIX_CATEGORY.items():
        if rule_id.startswith(prefix):
            return cat
    return "Best Practice"


def _ruff_severity(category: str) -> str:
    return {"Security": "High", "Bug": "Medium", "Complexity": "Medium"}.get(category, "Low")


def _eslint_category(rule_id: str) -> str:
    r = (rule_id or "").lower()
    for kw, cat in _ESLINT_KEYWORD_CATEGORY.items():
        if kw in r:
            return cat
    return "Code Style"


def _title_severity(mapped: str) -> str:
    return {"high": "High", "medium": "Medium", "low": "Low"}.get((mapped or "").lower(), "Low")


# --- normalization (per tool) ---------------------------------------------------------


def _norm_ruff(f: dict[str, Any]) -> dict[str, Any]:
    rule = str(f.get("rule_id", "")).strip()
    cat = _ruff_category(rule)
    return _finding(
        category=cat, severity=_ruff_severity(cat), source="Ruff", rule_id=rule,
        file=str(f.get("file", "")), line=_int(f.get("line")), column=_int(f.get("column")),
        message=_concise(cat, rule, str(f.get("message", ""))),
        tool_message=f"{rule} {f.get('message', '')}".strip(), evidence=str(f.get("evidence", "")),
    )


def _norm_eslint(f: dict[str, Any]) -> dict[str, Any]:
    rule = str(f.get("rule_id", "")).strip()
    cat = _eslint_category(rule)
    sev = "Medium" if int(f.get("severity", 1) or 1) == 2 else "Low"
    return _finding(
        category=cat, severity=sev, source="ESLint", rule_id=rule,
        file=str(f.get("file", "")), line=_int(f.get("line")), column=_int(f.get("column")),
        message=_concise(cat, rule, str(f.get("message", ""))),
        tool_message=f"{rule}: {f.get('message', '')}".strip(": "), evidence=str(f.get("evidence", "")),
    )


def _norm_sonar(f: dict[str, Any]) -> dict[str, Any]:
    rule = str(f.get("rule_id", "")).strip()
    cat = _SONAR_TYPE_CATEGORY.get(str(f.get("type", "")).upper(), "Maintainability")
    return _finding(
        category=cat, severity=_title_severity(str(f.get("severity", "low"))), source="SonarQube",
        rule_id=rule, file=str(f.get("file", "")), line=_int(f.get("line")), column=None,
        message=_concise(cat, rule, str(f.get("message", ""))),
        tool_message=str(f.get("message", "")).strip(), evidence="",
    )


def _finding(*, category: str, severity: str, source: str, rule_id: str, file: str,
             line: int | None, column: int | None, message: str, tool_message: str,
             evidence: str) -> dict[str, Any]:
    return {
        "id": "", "category": category, "severity": severity,
        "sources": [source], "rule_ids": [rule_id] if rule_id else [],
        "file": file, "line": line, "column": column,
        "message": message, "evidence": evidence,
        "tool_messages": [tool_message] if tool_message else [],
        "confidence": "Very High", "status": "Open",
        "suppressed_reason": "", "occurrences": 1, "additional_locations": [],
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


# --- stage 2: classification (suppression + severity refinement) ----------------------
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

# Severity corrections beyond the blanket category defaults, for well-known specific rules.
_SEVERITY_OVERRIDES = {
    "S101": "Medium",    # assert-used, production code only (test-path instances are suppressed)
    "S105": "High",      # hardcoded password/secret (when the value is NOT a known-safe constant)
    "S106": "High",
    "S608": "Critical",  # SQL injection via string-built query
    "S324": "High",      # insecure hash used for a security purpose
    "C901": "Medium",    # cyclomatic complexity over threshold
}


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


def classify(findings: list[dict[str, Any]]) -> None:
    """Apply deterministic suppression + severity rules to every finding, IN PLACE.

    Must run AFTER evidence has been filled in. Suppression only fires when EVERY rule attached
    to a (possibly merged) finding is in the known-suppressible set, so a finding that also
    carries an unrelated, non-suppressible rule is never hidden.
    """
    for f in findings:
        rule_ids = {r.upper() for r in f.get("rule_ids", [])}
        if not rule_ids:
            continue
        file, evidence = f.get("file", ""), f.get("evidence", "")

        if rule_ids.issubset(_TEST_ONLY_SUPPRESS_RULES) and _is_test_path(file):
            f["status"] = "Suppressed"
            f["suppressed_reason"] = (
                "Ruff S101 (assert-used) flags Python's `assert` statement because it is stripped "
                "when the interpreter runs with -O (optimized mode) - a real concern in production "
                "code. Inside test files, `assert` is the correct, standard mechanism pytest relies on."
            )
            f["confidence"] = "Low (auto-suppressed: likely false positive)"
            continue

        if rule_ids.issubset(_HARDCODED_SECRET_RULES) and _is_safe_secret_value(evidence):
            f["status"] = "Suppressed"
            f["suppressed_reason"] = (
                "Ruff S105/S106 flags any string literal assigned to a password/token/secret-"
                "looking variable name, regardless of the actual value. The captured value here is "
                "a known-safe constant (an auth-scheme name or an error-code whose value equals its "
                "own name), not a real secret."
            )
            f["confidence"] = "Low (auto-suppressed: likely false positive)"
            continue

        for rid in f.get("rule_ids", []):
            override = _SEVERITY_OVERRIDES.get(rid.upper())
            if override:
                f["severity"] = override
                break


# --- stage 3: rollup (collapse repeated suppressed findings) --------------------------


def rollup_suppressed(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeated SUPPRESSED findings sharing the same rule(s) into ONE row with an
    occurrence count (e.g. 450 pytest asserts -> one "S101 - 450 occurrences" row). ACTIONABLE
    findings are never rolled up - each needs its own attention and stays individually visible.
    Re-assigns sequential CR-### ids afterward so the final list is still cleanly numbered.
    """
    actionable = [f for f in findings if f.get("status") != "Suppressed"]
    suppressed = [f for f in findings if f.get("status") == "Suppressed"]

    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    order: list[tuple[str, ...]] = []
    for f in suppressed:
        key = tuple(sorted(f.get("rule_ids", [])))
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
    result.sort(key=lambda f: (f.get("status") == "Suppressed", -_SEV_RANK.get(f["severity"], 0),
                               f["file"], f["line"] or 0))
    for i, f in enumerate(result, start=1):
        f["id"] = f"CR-{i:03d}"
    return result


# --- root-cause / fix knowledge base (deterministic, per well-known rule) --------------

_RULE_KB: dict[str, dict[str, str]] = {
    "S101": {
        "why": "Python's `assert` statements are removed entirely when the interpreter runs with "
              "-O (optimized mode), silently disabling whatever check it performs.",
        "impact": "If this code path ever runs under `-O`, the assertion - and any side effect of "
                 "evaluating its condition - is skipped without warning.",
        "fix": "Replace with an explicit `if not condition: raise ...` for checks that must always "
              "run, regardless of interpreter flags.",
    },
    "S105": {
        "why": "A string literal is assigned to a variable whose name suggests a credential.",
        "impact": "If the value is a REAL secret, it is committed to source control in plaintext.",
        "fix": "Load real secrets from environment variables or a secrets manager - never hard-code them.",
    },
    "S106": {
        "why": "A string literal is passed as a password/token-like function argument.",
        "impact": "If the value is a REAL secret, it is committed to source control in plaintext.",
        "fix": "Load real secrets from environment variables or a secrets manager - never hard-code them.",
    },
    "F401": {
        "why": "The import is never referenced anywhere in the module.",
        "impact": "Dead code; adds noise and a small, unnecessary import-time cost.",
        "fix": "Remove the unused import.",
    },
    "F841": {
        "why": "A local variable is assigned but never read.",
        "impact": "Likely leftover/debug code, or a bug where the result was meant to be used.",
        "fix": "Remove the assignment, or use the variable if it was meant to be used.",
    },
    "C901": {
        "why": "The function's cyclomatic complexity exceeds the configured threshold.",
        "impact": "High-complexity functions are harder to test exhaustively and more bug-prone.",
        "fix": "Extract nested branches into smaller, named helper functions.",
    },
}


def rule_explanation(rule_ids: list[str]) -> dict[str, str] | None:
    """Deterministic why/impact/fix for a well-known rule; None when no canned entry exists
    (the renderer falls back to the tool's own message in that case)."""
    for rid in rule_ids:
        entry = _RULE_KB.get(rid.upper())
        if entry:
            return entry
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
