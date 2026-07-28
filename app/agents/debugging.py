"""Debugging Agent (LLM + tools) — the LLM-fix half of the post-commit debug/test loop.

Entered ONLY on a post-commit fixed-check failure: either the compile/build check
(``debug_result``) or the test suite (``test_result``). Per CLAUDE.md: the LLM proposes the fix
*content*; it may inspect the workspace via the repair tools (read-only git + install), but it
never executes the check and never commits. The repair tools are bound to the model THROUGH
``self.llm`` (``complete_with_tools``) so this module imports no provider SDK. Proposed file
content is then written back through the injected executor (fixed code disposes).

This node maintains the LOCAL ``debug_attempt`` counter and never touches ``repair_attempt`` or
the orchestrator's ``attempt``. Entered on a debug/test check failure; its job is to propose
corrected file content for the failure signal in state.

``debug_attempt`` is PROGRESS-SENSITIVE: it counts CONSECUTIVE rounds that failed to reduce the
failure count, and resets to 0 whenever a round actually made things better. It is deliberately
NOT a raw count of entries into this node. The flat per-entry counter it replaced conflated "this
loop is stuck" with "this project has a lot of independent failures", and on a large generated
project the latter is normal: one observed run had ~50 unrelated failing tests, so even a perfect
agent fixing 2-3 files per round could never finish inside a cap of 10 — while rounds that fixed
nothing at all (the model spending its whole tool budget exploring, then returning no parseable
fix) cost exactly as much cap as rounds that fixed three files. Counting only *stalled* rounds
lets a converging loop run to completion and still kills a genuinely stuck one promptly.
``DEBUG_ROUNDS_CEILING`` is the backstop for the pathological case progress-sensitivity alone
cannot catch: an oscillating loop whose every fix breaks something else, which would otherwise
reset the counter forever.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from app.agents.base import BaseAgent
from app.agents.code_generator import _extract_json, _project_dir, _project_path
from app.config.settings import get_settings
from app.graph.state import WorkflowState
from app.integrations.executor import Executor, get_executor
from app.services.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)

_DEBUG_REPORT_HEADER = (
    "# Debugging Report\n\n"
    "One section per round of the post-commit debug/test loop: what was failing, whether the round\n"
    "improved it, and exactly which files it rewrote. Rounds that touched a TEST file are marked as\n"
    "such — the agent may fix a faulty generated test, but only after concluding the source is\n"
    "correct, and it must say why in its notes (see app/prompts/debugging.md).\n"
)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value) or "run"


def _is_test_path(path: str) -> bool:
    """True for a test file — used to flag test edits distinctly in the report/summary."""
    base = path.rsplit("/", 1)[-1].lower()
    return ".test." in base or ".spec." in base or "/__tests__/" in path.lower()


def _parse_notes(raw: str) -> str:
    """The model's own "what I changed and why" string, for the report. Never fatal if absent."""
    obj = _extract_json(raw)
    if isinstance(obj, dict):
        notes = obj.get("notes")
        if isinstance(notes, str):
            return notes.strip()
    return ""

#: Tool-loop budget for the agentic fix session. A real fix commonly needs several read_file
#: calls plus install_package before it can write anything back — the gateway's generic 4-turn
#: fallback (see llm_gateway.complete_with_tools) starves that every time. Matches Refactoring's
#: REFACTOR_MAX_ITERS precedent rather than inheriting the tiny default.
DEBUG_MAX_ITERS = 16

#: Absolute backstop on TOTAL debugging rounds in a run, independent of the progress-sensitive
#: ``debug_attempt``. Progress-sensitivity alone cannot terminate an oscillating loop — one whose
#: every fix breaks something else, so the failure count alternates (30 -> 29 -> 30 -> 29) and
#: "progress" keeps resetting the stall counter. Deliberately generous: it exists to guarantee
#: termination, not to bound normal work.
DEBUG_ROUNDS_CEILING = 40


def _failure_count(state: WorkflowState) -> int:
    """How many things are currently failing — the progress signal for the debug/test loop.

    Prefers the test runner's own summary line, which is the only place a *quantified* failure
    count exists (jest: ``Tests: 341 failed, 4046 passed, 4387 total``; pytest: ``5 failed,
    10 passed``). Falls back to counting failed checks in the freshest ``GateResult`` when nothing
    parses — coarse (usually 1), but still monotonic enough to distinguish "fixed it" from "didn't",
    which is all the caller needs.

    Returns a LARGE sentinel rather than 0 when there is a failure it cannot quantify, so an
    unparseable failure is never mistaken for progress.
    """
    result = state.get("debug_result")
    if not (result and not result.get("passed", True)):
        result = state.get("test_result")
    if not (result and not result.get("passed", True)):
        return 0  # nothing failing

    checks = result.get("checks") or []
    blob = "\n".join(
        f"{c.get('stdout', '')}\n{c.get('stderr', '')}" for c in checks if not c.get("passed", True)
    )
    # Most specific first: the individual-test count is a finer-grained progress signal than the
    # suite count (fixing one assertion inside a still-failing suite is real progress).
    for pattern in (r"Tests:\s+(\d+)\s+failed", r"Test Suites:\s+(\d+)\s+failed", r"(\d+)\s+failed"):
        match = re.search(pattern, blob)
        if match:
            return int(match.group(1))

    failed_checks = sum(1 for c in checks if not c.get("passed", True))
    return failed_checks or 1


class DebuggingAgent(BaseAgent):
    name = "debugging"

    def __init__(self, executor: Executor | None = None, llm: LLMGateway | None = None) -> None:
        super().__init__()
        if llm is not None:
            self.llm = llm
        self._executor = executor

    def _resolve_executor(self) -> Executor:
        return self._executor if self._executor is not None else get_executor()

    def execute(self, state: WorkflowState) -> WorkflowState:
        # LOCAL, PROGRESS-SENSITIVE debug counter (NOT the same as repair_attempt or the
        # orchestrator's attempt). Measured against the failure count the PREVIOUS round left
        # behind: a round that reduced it resets the stall counter, a round that did not (or made
        # things worse, or wrote nothing at all) advances it toward the cap. See the module
        # docstring for why a flat per-entry counter was wrong here.
        failures_now = _failure_count(state)
        failures_before = int(state.get("debug_last_failure_count", -1))
        made_progress = failures_before < 0 or failures_now < failures_before
        state["debug_attempt"] = 0 if made_progress else int(state.get("debug_attempt", 0)) + 1
        state["debug_last_failure_count"] = failures_now
        state["debug_rounds"] = int(state.get("debug_rounds", 0)) + 1
        logger.info(
            "[debugging] run=%s | round %s: %s failing (was %s) — %s, stall counter now %s",
            state.get("run_id") or "-",
            state["debug_rounds"],
            failures_now,
            "n/a" if failures_before < 0 else failures_before,
            "progress" if made_progress else "NO progress",
            state["debug_attempt"],
        )

        executor = self._resolve_executor()
        # The debug path is entered only on a post-commit check failure: propose corrected file
        # content for the failing check's stderr. debug_result is always fresh (debug_check_node
        # overwrites it every run); test_result can be stale (only unit_test_run_node writes it),
        # so a failing debug_result always wins - see _current_failure.
        check_name, stderr, stdout = _current_failure(state)
        manifest = list(state.get("generated_code", []))

        system = self._load_prompt("debugging")
        prompt = self._build_prompt(check_name, stderr, stdout, manifest)
        # Tools are bound to the model inside the gateway; the model may inspect/install/diff.
        # Own iteration budget, not the gateway's generic default — see DEBUG_MAX_ITERS.
        raw = self.llm.complete_with_tools(
            prompt=prompt, system=system, tools=executor.get_repair_tools(), max_iters=DEBUG_MAX_ITERS
        )

        fixes = _parse_files(raw)
        written_paths: list[str] = []
        if fixes:
            # Write under the SAME <project_dir>/ prefix the code_generator used (and that the
            # completeness gate checks). Without this the fix writes a bare path the gate never
            # looks for, so the missing file stays "missing" and the loop burns to the cap.
            project_dir = _project_dir(state)
            generated = list(state.get("generated_code", []))
            for entry in fixes:
                path = _project_path(project_dir, entry["path"])
                executor.write_file(path, entry["content"])  # fixed code writes the proposal
                written_paths.append(path)
                if path not in generated:
                    generated.append(path)
            state["generated_code"] = generated
        else:
            # Proposal didn't parse: write nothing (no partial garbage). The check re-runs and
            # will re-fail/escalate; log it so the no-op fix is debuggable.
            logger.warning(
                "debugging: no valid fix parsed for run %s (attempt %s) — wrote nothing",
                state.get("run_id"),
                state.get("debug_attempt"),
            )
        self._record_round(
            state,
            check_name=check_name,
            failures_now=failures_now,
            failures_before=failures_before,
            made_progress=made_progress,
            written_paths=written_paths,
            notes=_parse_notes(raw),
        )
        # NO git_commit, NO check here — the graph routes back to the fixed check.
        return state

    # -- recording ---------------------------------------------------------------

    def _record_round(
        self,
        state: WorkflowState,
        *,
        check_name: str,
        failures_now: int,
        failures_before: int,
        made_progress: bool,
        written_paths: list[str],
        notes: str,
    ) -> None:
        """Append this round to ``generation_summary`` and the run's debugging report.

        Debugging used to be the ONE pipeline agent that left no durable trace: it wrote nothing to
        ``generation_summary`` and produced no report, so after a run finished there was no record
        of how many rounds ran, what they changed, or what they tried and failed at — the whole
        loop existed only as transient console output. That is doubly unacceptable now the agent is
        allowed to edit test files (see prompts/debugging.md): a change to what a test asserts must
        be auditable after the fact. Mirrors the report-writing shape of Code Review / Refactoring /
        Security (a .md under reports/<project>/ plus a ``*_report_path`` state field).
        """
        round_no = int(state.get("debug_rounds", 0))
        tests_touched = [p for p in written_paths if _is_test_path(p)]
        headline = (
            f"[debugging] round {round_no} ({check_name}): "
            f"{failures_now} failing"
            + ("" if failures_before < 0 else f" (was {failures_before})")
            + f" — {'progress' if made_progress else 'no progress'}, "
            + (f"{len(written_paths)} file(s) fixed" if written_paths else "wrote nothing")
            + (f", {len(tests_touched)} of them TEST file(s)" if tests_touched else "")
        )
        state["generation_summary"] = (state.get("generation_summary") or "") + headline + "\n"

        rows = "\n".join(f"| `{p}` | {'TEST' if _is_test_path(p) else 'source'} |" for p in written_paths)
        section = (
            f"\n## Round {round_no} — {check_name}\n\n"
            f"| Field | Value |\n| --- | --- |\n"
            f"| Failing before | {'(first measured round)' if failures_before < 0 else failures_before} |\n"
            f"| Failing now | {failures_now} |\n"
            f"| Progress | {'yes' if made_progress else 'NO'} |\n"
            f"| Stall counter | {state.get('debug_attempt', 0)} |\n\n"
            + (
                f"**Files changed ({len(written_paths)}):**\n\n| File | Kind |\n| --- | --- |\n{rows}\n"
                if written_paths
                else "**Files changed:** none — the model returned no parseable fix this round.\n"
            )
            + (f"\n**Agent notes:** {notes}\n" if notes else "")
        )
        report = (state.get("debugging_report") or _DEBUG_REPORT_HEADER) + section
        state["debugging_report"] = report

        # Writing to disk each round (rather than once at the end) means a run that crashes or is
        # killed mid-loop still leaves the rounds it did complete on disk — which is exactly when
        # this record is most needed.
        try:
            run_dir = Path(get_settings().reports_dir) / _slug(
                state.get("project_id") or state.get("run_id") or "run"
            )
            run_dir.mkdir(parents=True, exist_ok=True)
            md_path = run_dir / "debugging-report.md"
            md_path.write_text(report, encoding="utf-8")
            state["debugging_report_path"] = str(md_path)
        except Exception:  # noqa: BLE001 - a reporting failure must never break the fix loop
            logger.exception("debugging: failed to write the report for run %s", state.get("run_id"))

    @staticmethod
    def _build_prompt(check_name: str, stderr: str, stdout: str, manifest: list[str]) -> str:
        """Point the model at the failure + what exists, and let it pull content on demand.

        Previously this inlined the FULL content of every generated file (300+ files on a large
        run) regardless of relevance — burning hundreds of thousands of input tokens on a single
        failing check and leaving no realistic turn budget to actually fix anything. The stderr is
        the real signal; the file list is just a map of what's readable. The model has read_file
        (plus run_command/install_package/git_status/git_diff) via the repair tools and can fetch
        exactly the file(s) implicated by the failure instead of receiving all of them unread.

        Both stderr AND stdout are included: test runners (jest/vitest) commonly print the actual
        failing assertions/stack traces to stdout, with stderr carrying only warnings or nothing at
        all — stderr alone can leave the model with no real signal to act on.
        """
        files_list = "\n".join(f"- {path}" for path in manifest) or "(none on record)"
        return (
            f"The fixed {check_name} check failed.\n"
            f"Captured stderr:\n{stderr or '(none)'}\n\n"
            f"Captured stdout:\n{stdout or '(none)'}\n\n"
            "Generated file(s) in this project (call read_file on whichever ones the failure "
            f"above implicates — do not assume you need all of them):\n{files_list}\n\n"
            'Return the corrected file(s) as STRICT JSON: {"files":[{"path":...,"content":...}],"notes":...}'
        )


def _current_failure(state: WorkflowState) -> tuple[str, str, str]:
    """Pick the freshest failure signal: (check_name, stderr, stdout).

    ``debug_result`` is always current: ``debug_check_node`` overwrites it every time it runs.
    ``test_result`` is NOT always current: only ``unit_test_run_node`` writes it, so it can still
    hold a stale failure from an earlier loop iteration after a later fix changes what actually
    fails. A failing ``debug_result`` is therefore always the live signal when present - check it
    first. Falling back to ``test_result`` is still correct for a genuine test failure: reaching
    ``unit_test_run`` at all requires ``debug_result`` to have been passing at that point (see
    ``route_after_debug_check``), so it won't shadow a real test failure.
    """
    debug_result = state.get("debug_result")
    if debug_result and not debug_result.get("passed", True):
        stderr, stdout = _first_failure_output(debug_result)
        return "compile/build", stderr, stdout
    test_result = state.get("test_result")
    if test_result and not test_result.get("passed", True):
        stderr, stdout = _first_failure_output(test_result)
        return "test", stderr, stdout
    return "unknown", "", ""


def _first_failure_output(gate_result: Any) -> tuple[str, str]:
    """(stderr, stdout) of the first failing check. Test runners (jest/vitest) commonly print
    the actual failing assertions to stdout, not stderr — both must reach the model."""
    for check in gate_result.get("checks", []):
        if not check.get("passed", True):
            return str(check.get("stderr", "")), str(check.get("stdout", ""))
    return "", ""


def _parse_files(raw: str) -> list[dict[str, str]] | None:
    obj = _extract_json(raw)
    if not isinstance(obj, dict) or not isinstance(obj.get("files"), list):
        return None
    clean: list[dict[str, str]] = []
    for entry in obj["files"]:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and isinstance(entry.get("content"), str):
            clean.append({"path": entry["path"], "content": entry["content"]})
    return clean or None


# Module-level agent reused across invocations (guide's node pattern). Executor + gateway are
# resolved at run time (provider / singleton), so tests inject via set_executor / monkeypatch.
_debugging_agent = DebuggingAgent()


def debugging_node(state: WorkflowState) -> WorkflowState:
    logger.info("================ AGENT: Debugging ================")
    logger.info("   -> fixing the failing compile/build/test check via the LLM repair path")
    return _debugging_agent.execute(state)
