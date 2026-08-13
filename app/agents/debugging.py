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

Also owns a running report (``debugging_report`` / ``debugging_report_path``, mirroring Code
Review's/Security's report fields) that ACCUMULATES one section per round and is rewritten to
disk on every call — so a run that hits the cap and escalates still leaves a full record of what
each round tried, not just the last one.
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
from app.services.plan_builder import _is_test_path
from app.services.wiring import UnresolvedImport, target_resolves

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

#: Ceiling on how many unresolved-import findings get listed in one prompt. The list is only ever
#: pruned by THIS agent fixing an entry (see ``_prune_unresolved``), so an unusually large finding
#: set would otherwise inject an unbounded, ever-growing block into every round's prompt.
_MAX_UNRESOLVED_IN_PROMPT = 50


def _render_unresolved(item: dict[str, Any]) -> str:
    """Render one ``WorkflowState.unresolved_imports`` entry (a plain dict) via
    ``UnresolvedImport.as_note()`` — the single source of truth for that note's wording — rather
    than re-implementing the format string here."""
    return UnresolvedImport(
        importer=str(item.get("importer", "")),
        specifier=str(item.get("specifier", "")),
        target=str(item.get("target", "")),
        candidates=tuple(item.get("candidates") or ()),
    ).as_note()


def _prune_unresolved(
    unresolved: list[dict[str, Any]],
    *,
    project_dir: str,
    generated_code: list[str],
    fixes: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Drop an unresolved-import finding once THIS round plausibly resolved it, so the next round
    isn't handed a fix-this instruction for an import that's already fixed.

    Two repair shapes, matching the two the reconcile pass itself documents (wiring.py):
      - "create the missing module": the finding's ``target`` now resolves against the current
        ``generated_code`` path set (the round wrote the missing file, or a PRIOR round did and
        nothing has pruned it yet).
      - "rename the importer": the round rewrote the importer, and the specifier text is no longer
        present in what it wrote.

    Conservative both ways: an entry is kept unless one of these is a clean hit, so a partial or
    failed fix simply resurfaces next round rather than being silently dropped.
    """
    prefix = f"{project_dir}/"
    existing = {p[len(prefix):] if p.startswith(prefix) else p for p in generated_code}
    rewritten = {entry["path"]: entry["content"] for entry in fixes}

    kept: list[dict[str, Any]] = []
    for item in unresolved:
        target = str(item.get("target", ""))
        if target_resolves(target, existing):
            continue  # the missing module now exists
        importer = str(item.get("importer", ""))
        # `rewritten` keys are exactly the paths the model returned, which may not share the
        # reconcile pass's project-relative prefix convention byte-for-byte — match by suffix so
        # a minor path-form difference doesn't defeat the check.
        new_content = next(
            (c for p, c in rewritten.items() if p == importer or importer.endswith(p) or p.endswith(importer)),
            None,
        )
        if new_content is not None and str(item.get("specifier", "")) not in new_content:
            continue  # importer was rewritten and no longer imports the missing specifier
        kept.append(item)
    return kept


def _failure_count(state: WorkflowState) -> int:
    """How many things are currently failing — the progress signal for the debug/test loop.

    Prefers the test runner's own summary line, which is the only place a *quantified* failure
    count exists (jest: ``Tests: 341 failed, 4046 passed, 4387 total``; pytest: ``5 failed,
    10 passed``). Falls back to counting failed checks in the freshest ``GateResult`` when nothing
    parses — coarse (usually 1), but still monotonic enough to distinguish "fixed it" from "didn't",
    which is all the caller needs.

    A count from THIS function is only ever compared against a PREVIOUS count of the SAME check
    kind (see ``execute``'s kind-gating) — comparing a quantified test-failure count against the
    coarse "1" a compile failure falls back to would misread a regression (test -> compile break)
    as dramatic progress, since both numbers come from unrelated scales.
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
    # suite count (fixing one assertion inside a still-failing suite is real progress). Anchored to
    # line start (MULTILINE) so a bare "N failed" doesn't latch onto an unrelated number quoted
    # inside a stack trace or a snapshot diff — jest/pytest both print their summary as its own line.
    for pattern in (r"Tests:\s+(\d+)\s+failed", r"Test Suites:\s+(\d+)\s+failed", r"^\s*(\d+)\s+failed"):
        match = re.search(pattern, blob, re.MULTILINE)
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
        executor = self._resolve_executor()
        # The debug path is entered only on a post-commit check failure: propose corrected file
        # content for the failing check's stderr. debug_result is always fresh (debug_check_node
        # overwrites it every run); test_result can be stale (only unit_test_run_node writes it),
        # so a failing debug_result always wins - see _current_failure.
        check_name, stderr, stdout = _current_failure(state)
        if check_name == "unknown":
            # Neither debug_result nor test_result is actually failing. This shouldn't normally
            # happen given the router (this node is only entered on a real check failure), but
            # nothing here enforces that, and the cost of being wrong is a full DEBUG_MAX_ITERS-turn
            # LLM round burned for zero signal ("The fixed unknown check failed... (none)... (none)").
            # Treat it as a cheap no-op instead: no LLM call, no debug_rounds/debug_attempt bump, no
            # report entry — state is returned exactly as received.
            logger.warning(
                "[debugging] run=%s | _current_failure returned 'unknown' (neither debug_result "
                "nor test_result is failing) — skipping the LLM round entirely",
                state.get("run_id") or "-",
            )
            return state
        manifest = list(state.get("generated_code", []))

        # LOCAL, PROGRESS-SENSITIVE debug counter (NOT the same as repair_attempt or the
        # orchestrator's attempt). Measured against the failure count the PREVIOUS round left
        # behind: a round that reduced it resets the stall counter, a round that did not (or made
        # things worse, or wrote nothing at all) advances it toward the cap. See the module
        # docstring for why a flat per-entry counter was wrong here.
        #
        # KIND-GATED: a lower count only counts as progress when this round's check_name matches
        # the PREVIOUS round's — comparing counts across kinds compares different units (a compile
        # failure is always reported as a bare "1"), so a test suite regressing from "30 failing"
        # to a broken build would otherwise read as "1 < 30 -> progress" and wrongly reset the
        # stall counter right when the loop actually got worse. DEBUG_ROUNDS_CEILING still bounds
        # the case where the kind keeps flip-flopping (kind-gating alone can't terminate that).
        #
        # A KIND CHANGE ITSELF (check_name != kind_before) is NEUTRAL for the stall counter — it
        # must NOT increment it. The most common case is FORWARD: compile/build finally passes and
        # test runs for the first time — that is the loop advancing to a new phase, not failing to
        # make progress on the current one, and incrementing here shaved a round off the budget for
        # a check that had never even run before. It must also NOT reset the counter to 0: the
        # BACKWARD case (a fix breaks the build entirely, so debug_result starts failing again
        # after test was failing) is precisely the regression kind-gating above exists to catch —
        # resetting on every kind change would let that exact oscillation launder an elevated
        # counter back to 0 on every swing, silently defeating kind-gating and DEBUG_ROUNDS_CEILING
        # both. So on a kind change the counter is left EXACTLY as it was; only the recorded
        # (count, kind) baseline updates, so the NEXT round can compare correctly.
        failures_now = _failure_count(state)
        failures_before = int(state.get("debug_last_failure_count", -1))
        kind_before = state.get("debug_last_failure_kind") or ""
        first_round = failures_before < 0
        kind_changed = not first_round and check_name != kind_before
        made_progress = first_round or (not kind_changed and failures_now < failures_before)
        if not kind_changed:
            state["debug_attempt"] = 0 if made_progress else int(state.get("debug_attempt", 0)) + 1
        state["debug_last_failure_count"] = failures_now
        state["debug_last_failure_kind"] = check_name
        state["debug_rounds"] = int(state.get("debug_rounds", 0)) + 1
        logger.info(
            "[debugging] run=%s | round %s: %s failing [%s] (was %s%s) — %s, stall counter now %s",
            state.get("run_id") or "-",
            state["debug_rounds"],
            failures_now,
            check_name,
            "n/a" if first_round else failures_before,
            "" if first_round else f" [{kind_before}]",
            "kind changed (neutral)" if kind_changed else ("progress" if made_progress else "NO progress"),
            state["debug_attempt"],
        )

        system = self._load_prompt("debugging")
        unresolved = list(state.get("unresolved_imports", []))
        prompt = self._build_prompt(check_name, stderr, stdout, manifest, unresolved)
        # Tools are bound to the model inside the gateway; the model may inspect/install/diff.
        # Own iteration budget, not the gateway's generic default — see DEBUG_MAX_ITERS.
        raw = self.llm.complete_with_tools(
            prompt=prompt, system=system, tools=executor.get_repair_tools(), max_iters=DEBUG_MAX_ITERS
        )

        fixes, parse_error = _parse_files(raw)
        if fixes is None:
            # Hitting DEBUG_MAX_ITERS without a parseable reply is a GUARANTEED wasted round: when
            # the tool loop exhausts max_iters, llm_gateway.complete_with_tools returns whatever
            # prose text accompanied the LAST turn's tool_use blocks (e.g. "Now let me check the
            # middleware config..."), never the JSON fix — so nothing here would ever parse anyway.
            # ONE additional tool-FREE retry (self.llm.complete, no tools bound) explicitly re-asks
            # for the JSON — costs one cheap call instead of accepting a wasted DEBUG_MAX_ITERS-turn
            # round. Matches the parse-failure retry unit_test.py / code_generator.py already use.
            retry_prompt = (
                f"{prompt}\n\nYour previous reply was not valid JSON matching "
                f'{{"files":[{{"path":...,"content":...}}]}}. Error: {parse_error}. '
                "Reply with STRICT JSON only — no prose, no code fences."
            )
            raw = self.llm.complete(prompt=retry_prompt, system=system)
            fixes, parse_error = _parse_files(raw)

        written_paths: list[str] = []
        if fixes:
            # Write under the SAME <project_dir>/ prefix the code_generator used (and that the
            # completeness gate checks). Without this the fix writes a bare path the gate never
            # looks for, so the missing file stays "missing" and the loop burns to the cap.
            project_dir = _project_dir(state)
            generated = list(state.get("generated_code", []))
            for entry in fixes:
                path = _project_path(project_dir, entry["path"])
                try:
                    executor.write_file(path, entry["content"])  # fixed code writes the proposal
                except Exception:  # noqa: BLE001 - one bad model-proposed path (e.g. a traversal
                    # path LocalDiskExecutor._resolve rejects) must not sink every OTHER legitimate
                    # fix in this round; skip just this file and keep going.
                    logger.exception(
                        "debugging: failed to write fix %r for run %s — skipping this file, "
                        "continuing with the rest of the round",
                        path, state.get("run_id"),
                    )
                    continue
                written_paths.append(path)
                if path not in generated:
                    generated.append(path)
            state["generated_code"] = generated
            logger.info(
                "[debugging] run=%s | round %s: wrote %d fixed file(s): %s",
                state.get("run_id") or "-", state["debug_rounds"], len(written_paths), ", ".join(written_paths),
            )
        else:
            # Proposal didn't parse even after the retry: write nothing (no partial garbage). The
            # check re-runs and will re-fail/escalate; log it so the no-op fix is debuggable.
            logger.warning(
                "debugging: no valid fix parsed for run %s (attempt %s) even after the tool-free "
                "retry (%s) — wrote nothing",
                state.get("run_id"),
                state.get("debug_attempt"),
                parse_error,
            )
        if unresolved:
            # Re-check the reconcile pass's findings against what THIS round changed, so a later
            # round isn't handed the same "fix this" list for an import that was already fixed —
            # see _prune_unresolved for exactly what counts as resolved.
            state["unresolved_imports"] = _prune_unresolved(
                unresolved, project_dir=_project_dir(state),
                generated_code=state.get("generated_code", []), fixes=fixes or [],
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
    def _build_prompt(
        check_name: str,
        stderr: str,
        stdout: str,
        manifest: list[str],
        unresolved_imports: list[dict[str, Any]] | None = None,
    ) -> str:
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

        ``unresolved_imports`` (plain dicts from ``WorkflowState`` — see
        ``UnresolvedImport.to_dict()``) is included because a bundler reports only the FIRST
        unresolvable import and then stops: fixing it just reveals the next one, so N broken
        imports cost N build-fail/fix rounds. Handing over the whole pre-computed list lets one
        round fix all of them. Capped at ``_MAX_UNRESOLVED_IN_PROMPT`` entries — the list is
        state that only ever grows within a run (nothing prunes an entry except this agent fixing
        it), and an uncapped one is the same unbounded-prompt hazard ``scripts/local_executor.py``
        caps for captured command output.
        """
        files_list = "\n".join(f"- {path}" for path in manifest) or "(none on record)"
        unresolved_block = ""
        if unresolved_imports:
            shown = unresolved_imports[:_MAX_UNRESOLVED_IN_PROMPT]
            listed = "\n".join(f"- {_render_unresolved(item)}" for item in shown)
            overflow = len(unresolved_imports) - len(shown)
            if overflow > 0:
                listed += f"\n- ... and {overflow} more (fix these first; the rest resurface next round)"
            unresolved_block = (
                f"\nA deterministic pre-pass found {len(unresolved_imports)} relative import(s) that "
                "resolve to NO generated file. The check above may only be reporting the first one; "
                "these are very likely the same root cause and you should fix as many as you can in "
                "this one reply — either create the missing module or correct the importer, "
                "whichever matches the conventions the rest of the project already uses:\n"
                f"{listed}\n"
            )
        return (
            f"The fixed {check_name} check failed.\n"
            f"Captured stderr:\n{stderr or '(none)'}\n\n"
            f"Captured stdout:\n{stdout or '(none)'}\n"
            f"{unresolved_block}\n"
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


def _parse_files(raw: str) -> tuple[list[dict[str, str]] | None, str]:
    """Parse the model reply into a list of {path, content}. Returns (files, error) — the error
    string feeds the D1 tool-free retry prompt (see ``execute``) so the re-ask names the SPECIFIC
    thing that was wrong, matching ``unit_test.py``/``code_generator.py``'s own ``_parse``."""
    obj = _extract_json(raw)
    if not isinstance(obj, dict):
        return None, "no JSON object found in reply"
    files = obj.get("files")
    if not isinstance(files, list) or not files:
        return None, "'files' must be a non-empty array"
    clean: list[dict[str, str]] = []
    for entry in files:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("content"), str)
        ):
            return None, "each file needs string 'path' and 'content'"
        clean.append({"path": entry["path"], "content": entry["content"]})
    return clean, ""


# Module-level agent reused across invocations (guide's node pattern). Executor + gateway are
# resolved at run time (provider / singleton), so tests inject via set_executor / monkeypatch.
_debugging_agent = DebuggingAgent()


def debugging_node(state: WorkflowState) -> WorkflowState:
    logger.info("================ AGENT: Debugging ================")
    logger.info("   -> fixing the failing compile/build/test check via the LLM repair path")
    return _debugging_agent.execute(state)
