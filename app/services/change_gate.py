"""Did the change actually happen, and ONLY the change? — deterministic, no LLM.

The existing gate is ``files_complete``: does every target path exist on disk? For greenfield that
is the right question, because the files did not exist a moment ago. For brownfield it is
vacuous — every target already exists, so the gate passes on the first pass whether or not the
editing agent wrote anything, and a run can report success with a zero-line diff.

This module asks the two questions that actually matter, both answered from content digests taken
before any edit:

1. **Did each target change in the way its action claims?** ``modify`` requires the content to
   differ from the baseline; ``create`` requires the file to now exist.
2. **Did anything ELSE change?** Any file whose digest moved but which no work item claimed is a
   failure, naming the paths.

The second check is what gives the gate teeth. It is the only thing standing between "the model
edited the file it was asked to" and "the model also rewrote three others while it was in there" —
the collateral-rewrite failure that whole-file overwrites make easy and that a reviewer of a large
diff will not reliably catch.

Digests come from executor reads, so this behaves identically on ``FakeExecutor``,
``LocalDiskExecutor`` and ``MCPExecutor``, needs no git, and is fully testable in memory. It
deliberately produces the SAME ``GateCheck``/``GateResult`` shape ``gate_node`` produces, so the
existing repair accounting, routing and escalation work over it unchanged.
"""

from __future__ import annotations

import hashlib

from app.graph.state import GateCheck, GateResult
from app.integrations.executor import Executor
from app.services.change_plan import normalize_target


def digest_of(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


def current_digests(executor: Executor, project_dir: str, paths: list[str]) -> dict[str, str]:
    """sha256 per project-relative path RIGHT NOW; ``""`` for absent/unreadable.

    Absent and unreadable both mean "no content", and both are legitimate states here (a ``create``
    target does not exist yet). Keeping the key rather than dropping it means a later comparison
    still sees the path.
    """
    out: dict[str, str] = {}
    for rel in paths:
        try:
            out[rel] = digest_of(executor.read_file(f"{project_dir}/{rel}"))
        except Exception:  # noqa: BLE001 - absent/unreadable == no content
            out[rel] = ""
    return out


def _check(name: str, passed: bool, stderr: str = "") -> GateCheck:
    return {"name": name, "passed": passed, "stderr": stderr, "stdout": "",
            "exit_code": 0 if passed else 1, "scope": ""}


def evaluate(
    executor: Executor,
    project_dir: str,
    *,
    action: str,
    target_files: list[str],
    baseline_digests: dict[str, str],
    claimed_paths: set[str],
) -> GateResult:
    """Judge one work item's edit against the pre-change baseline.

    ``baseline_digests`` covers the WHOLE repository as acquired (not just this item's targets), so
    the blast-radius check can see a file nobody planned to touch. ``claimed_paths`` is every path
    the accepted plan claims across all items — a file another item legitimately owns must not be
    reported as collateral damage here.
    """
    targets = [normalize_target(p) for p in target_files]
    now = current_digests(executor, project_dir, sorted(baseline_digests) + targets)
    checks: list[GateCheck] = []

    # 1. Each target changed as its action claims.
    unchanged, missing = [], []
    for rel in targets:
        before = baseline_digests.get(rel, "")
        after = now.get(rel, "")
        if action == "create":
            if not after:
                missing.append(rel)
        elif not after:
            missing.append(rel)          # a modify target that vanished
        elif after == before:
            unchanged.append(rel)

    if missing:
        checks.append(_check(
            "files_present", False,
            f"target file(s) not written: {', '.join(missing)}",
        ))
    else:
        checks.append(_check("files_present", True))

    if unchanged:
        checks.append(_check(
            "files_changed", False,
            "no change detected in: " + ", ".join(unchanged)
            + " - the file(s) are byte-identical to before the edit. Re-read them and apply the "
              "change described in the work item, or say why it cannot be made there.",
        ))
    else:
        checks.append(_check("files_changed", True))

    # 2. Nothing outside the plan changed.
    collateral = sorted(
        rel for rel, before in baseline_digests.items()
        if rel not in claimed_paths and now.get(rel, "") != before
    )
    if collateral:
        checks.append(_check(
            "no_collateral_changes", False,
            "file(s) changed that no work item claimed: " + ", ".join(collateral[:10])
            + (f" (+{len(collateral) - 10} more)" if len(collateral) > 10 else "")
            + " - restore them to their original content and confine the edit to the target files.",
        ))
    else:
        checks.append(_check("no_collateral_changes", True))

    return {"passed": all(c["passed"] for c in checks), "checks": checks}


def classify_regressions(baseline: dict, current: dict) -> dict[str, list[str]]:
    """Split check outcomes into regressed / fixed / pre-existing.

    A brownfield repository is not obliged to be green when we find it. Without this split, a repo
    with three already-failing tests burns the entire debug budget trying to fix code the run never
    touched, then escalates — so only a check that PASSED before and fails now should gate.

    ``baseline`` and ``current`` map check name -> passed. A check missing from ``baseline`` is
    "unknown", reported separately rather than assumed green.
    """
    regressed, fixed, preexisting, unknown = [], [], [], []
    for name, now_ok in sorted(current.items()):
        if name not in baseline:
            unknown.append(name)
        elif baseline[name] and not now_ok:
            regressed.append(name)
        elif not baseline[name] and now_ok:
            fixed.append(name)
        elif not baseline[name] and not now_ok:
            preexisting.append(name)
    return {"regressed": regressed, "fixed": fixed,
            "preexisting": preexisting, "unknown": unknown}
