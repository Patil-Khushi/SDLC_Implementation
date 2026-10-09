"""Debugging Agent (LLM + tools) — the LLM-fix half of the post-commit debug/test loop.

Entered ONLY on a post-commit fixed-check failure: either the compile/build check
(``debug_result``) or the test suite (``test_result``). Per CLAUDE.md: the LLM proposes the fix
*content*; it may inspect the workspace (read-only git, install, scoped run_command), but it never
executes the check and never commits. All tools are bound to the model THROUGH ``self.llm``
(``complete_with_tools``) so this module imports no provider SDK.

**Fixes land through tools, as they are made.** The model edits files with the shared
``write_file`` / ``edit_file`` tools (``app/agents/editing.py`` — the same guarded pair Refactoring
and the Change Modifier use), and every edit is written the moment it is made. The agent used to
require ONE strict-JSON reply carrying the full content of every fixed file; on a real project
(127 TypeScript errors across 43 files) that reply could not fit in the output budget, came back
cut off, failed to parse, and the WHOLE round was discarded — rounds 2-9 of every such run wrote
nothing. A JSON ``{"files": [...]}`` final reply is still honoured as a fallback, so nothing that
worked before stops working.

**The prompt shows the whole failure.** Every failing check and every failing tool inside it (not
just the first), stdout de-duplicated against stderr, the errors grouped by file worst-first, and
any sign the failure is ENVIRONMENTAL (no C compiler, no network, a timeout) — see
``app/services/check_digest.py``. A ``pip`` failure that no code edit can fix used to be invisible
behind the first failing ``tsc`` block.

This node maintains the LOCAL ``debug_attempt`` counter and never touches ``repair_attempt`` or
the orchestrator's ``attempt``.

``debug_attempt`` is PROGRESS-SENSITIVE: it counts CONSECUTIVE rounds that failed to reduce the
failure count, and resets to 0 whenever a round actually made things better. It is deliberately
NOT a raw count of entries into this node. The flat per-entry counter it replaced conflated "this
loop is stuck" with "this project has a lot of independent failures", and on a large generated
project the latter is normal. The failure count is now QUANTIFIED from the tools' own output (the
number of ``tsc`` error lines, the test runner's "N failed") rather than "how many checks failed",
which was 2 whether 209 errors remained or 109 — so a converging loop was read as stalled.
``DEBUG_ROUNDS_CEILING`` is the backstop for the pathological case progress-sensitivity alone
cannot catch: an oscillating loop whose every fix breaks something else.

Also owns a running report (``debugging_report`` / ``debugging_report_path``, mirroring Code
Review's/Security's report fields) that ACCUMULATES one section per round and is rewritten to
disk on every call — so a run that hits the cap and escalates still leaves a full record of what
each round tried, why a round wrote nothing (LLM error, stop reason), and what looked environmental.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from app.agents.base import BaseAgent
from app.agents.code_generator import _extract_json, _project_dir, _project_path
from app.agents.editing import editing_tools
from app.config.settings import get_settings
from app.graph.state import WorkflowState
from app.integrations.executor import Executor, cap_output, get_executor
from app.services import check_digest
from app.services.llm_gateway import LLMGateway
from app.services.plan_builder import _is_test_path
from app.services.python_wiring import defined_names
from app.services.wiring import UnresolvedImport, target_resolves

logger = logging.getLogger(__name__)

_DEBUG_REPORT_HEADER = (
    "# Debugging Report\n\n"
    "One section per round of the post-commit debug/test loop: what was failing, whether the round\n"
    "improved it, and exactly which files it rewrote. Rounds that touched a TEST file are marked as\n"
    "such — the agent may fix a faulty generated test, but only after concluding the source is\n"
    "correct, and it must say why in its notes (see app/prompts/debugging.md).\n"
)

#: Tool-loop budget FLOOR for one fix session. A real fix commonly needs several read_file calls
#: before it can edit anything; ``_max_iters_for`` scales it up with the number of files that have
#: errors, so a 40-file failure is not starved at the same budget as a one-line import fix.
DEBUG_MAX_ITERS = 16
#: Extra tool turns per file with errors (one read + one edit), on top of the floor.
DEBUG_ITERS_PER_FILE = 2
#: Ceiling on the scaled budget. Each turn re-sends the whole conversation, so an unbounded loop is
#: a cost and context-window hazard; the check re-runs after every round anyway, so files left
#: unfixed simply come back next round.
DEBUG_MAX_ITERS_CEILING = 48

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
#: How many worst files the prompt's "errors by file" table lists.
_TOP_FILES_IN_PROMPT = 25
#: How many files a round is ASKED to aim for. Not enforced — a cheap fix in a 13th file is fine —
#: but saying it keeps one round's conversation (and its re-sent context) bounded.
_FILES_PER_ROUND_HINT = 12
#: Longest model summary kept in the report.
_NOTES_CAP = 2000


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value) or "run"


def _parse_notes(raw: str) -> str:
    """The model's own "what I changed and why", for the report. With tool-based fixes the final
    reply IS that summary in plain text; a legacy JSON reply carries it in ``notes``."""
    obj = _extract_json(raw or "")
    if isinstance(obj, dict) and isinstance(obj.get("notes"), str):
        return obj["notes"].strip()[:_NOTES_CAP]
    text = (raw or "").strip()
    return "" if text.startswith("{") else text[:_NOTES_CAP]


def _render_unresolved(item: dict[str, Any]) -> str:
    """Render one ``WorkflowState.unresolved_imports`` entry (a plain dict) via
    ``UnresolvedImport.as_note()`` — the single source of truth for that note's wording — rather
    than re-implementing the format string here."""
    return UnresolvedImport(
        importer=str(item.get("importer", "")),
        specifier=str(item.get("specifier", "")),
        target=str(item.get("target", "")),
        candidates=tuple(item.get("candidates") or ()),
        name=str(item.get("name", "")),
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

    def _rewritten(path: str) -> str | None:
        # `rewritten` keys are exactly the paths the model returned, which may not share the
        # reconcile pass's project-relative prefix convention byte-for-byte — match by suffix so
        # a minor path-form difference doesn't defeat the check.
        return next(
            (c for p, c in rewritten.items() if p == path or path.endswith(p) or p.endswith(path)),
            None,
        )

    kept: list[dict[str, Any]] = []
    for item in unresolved:
        target = str(item.get("target", ""))
        importer = str(item.get("importer", ""))
        name = str(item.get("name", ""))
        if name:
            # A missing NAME in a module that exists: the module existing proves nothing. Resolved
            # when the module now defines it, or the importer no longer imports it.
            target_content = _rewritten(target)
            if target_content is not None and name in (defined_names(target_content) or {name}):
                continue
            importer_content = _rewritten(importer)
            if importer_content is not None and not re.search(rf"\b{re.escape(name)}\b", importer_content):
                continue
            kept.append(item)
            continue
        if target_resolves(target, existing):
            continue  # the missing module now exists
        new_content = _rewritten(importer)
        if new_content is not None and str(item.get("specifier", "")) not in new_content:
            continue  # importer was rewritten and no longer imports the missing specifier
        kept.append(item)
    return kept


def _failing_checks(state: WorkflowState) -> tuple[str, list[dict[str, Any]]]:
    """``(kind, every failing check)`` for the freshest failure signal.

    Same precedence as :func:`_current_failure` (a failing ``debug_result`` always wins over a
    possibly-stale ``test_result``), but returns ALL failing checks of that result — a debug_check
    is compile AND build, and a build failure behind a compile failure used to be invisible.
    """
    debug_result = state.get("debug_result")
    if debug_result and not debug_result.get("passed", True):
        return "compile/build", [c for c in debug_result.get("checks", []) if _code_failure(c)]
    test_result = state.get("test_result")
    if test_result and not test_result.get("passed", True):
        return "test", [c for c in test_result.get("checks", []) if _code_failure(c)]
    return "unknown", []


def _code_failure(check: dict[str, Any]) -> bool:
    """A failing check the CODE may have caused. An inconclusive one (every tool failed for an
    environment reason) is left out: no edit can fix a missing compiler or an unreachable registry."""
    return not check.get("passed", True) and not check.get("inconclusive")


def _failure_count(state: WorkflowState) -> int:
    """How many things are currently failing — the progress signal for the debug/test loop.

    QUANTIFIED from the failing tools' own output (``check_digest.digest_checks``): a test runner's
    summary (jest ``Tests: 341 failed``, pytest ``5 failed, 2 errors``), ``tsc``'s error count (its
    error LINES when, non-interactive, it prints no summary), and 1 for any failing tool that
    printed neither. Summed across every failing tool, so "120 type errors and a failed pip
    install" is 121 and fixing either half is visible progress.

    A count from THIS function is only ever compared against a PREVIOUS count of the SAME check
    kind (see ``execute``'s kind-gating) — comparing a test-failure count against a compile-error
    count would compare unrelated scales.
    """
    _kind, failing = _failing_checks(state)
    if not failing:
        return 0  # nothing failing
    return check_digest.digest_checks(failing).error_count or 1


class DebuggingAgent(BaseAgent):
    name = "debugging"

    def __init__(self, executor: Executor | None = None, llm: LLMGateway | None = None) -> None:
        super().__init__()
        if llm is not None:
            self.llm = llm
        self._executor = executor

    def _resolve_executor(self) -> Executor:
        return self._executor if self._executor is not None else get_executor()

    @staticmethod
    def _max_iters_for(file_count: int) -> int:
        scaled = DEBUG_MAX_ITERS + DEBUG_ITERS_PER_FILE * max(0, file_count)
        return max(DEBUG_MAX_ITERS, min(scaled, DEBUG_MAX_ITERS_CEILING))

    def _tools(self, executor: Executor, project_dir: str, touched: list[str]) -> list[Any]:
        """``read_file`` / ``write_file`` / ``edit_file`` scoped to the project, plus the executor's
        inspection tools (install, scoped run_command, git status/diff).

        The executor's own ``read_file`` is dropped: it takes WORKSPACE-relative paths while the
        editing tools take project-relative ones, and two tools with one name is ambiguous for the
        model (the provider would see a duplicate name).
        """
        tools = editing_tools(executor, project_dir, touched, label="debugging", edit=True)
        taken = {getattr(t, "name", "") for t in tools}
        return tools + [t for t in executor.get_repair_tools() if getattr(t, "name", "") not in taken]

    def execute(self, state: WorkflowState) -> WorkflowState:
        executor = self._resolve_executor()
        check_name, failing = _failing_checks(state)
        if check_name == "unknown":
            # Neither debug_result nor test_result is actually failing. This shouldn't normally
            # happen given the router (this node is only entered on a real check failure), but
            # nothing here enforces that, and the cost of being wrong is a full LLM round burned
            # for zero signal. Treat it as a cheap no-op instead: no LLM call, no
            # debug_rounds/debug_attempt bump, no report entry — state is returned as received.
            logger.warning(
                "[debugging] run=%s | no failing debug_result or test_result — skipping the LLM "
                "round entirely",
                state.get("run_id") or "-",
            )
            return state
        manifest = list(state.get("generated_code", []))
        project_dir = _project_dir(state)

        # LOCAL, PROGRESS-SENSITIVE debug counter (NOT the same as repair_attempt or the
        # orchestrator's attempt). Measured against the failure count the PREVIOUS round left
        # behind: a round that reduced it resets the stall counter, a round that did not (or made
        # things worse, or wrote nothing at all) advances it toward the cap.
        #
        # KIND-GATED: a lower count only counts as progress when this round's check_name matches
        # the PREVIOUS round's — counts of different kinds are different units, so a test suite
        # regressing to a broken build must not read as progress.
        #
        # A KIND CHANGE ITSELF is NEUTRAL for the stall counter. The common case is FORWARD
        # (compile/build finally passes and the tests run for the first time) — the loop advancing,
        # not stalling. It must also NOT reset the counter: the BACKWARD case (a fix breaks the build
        # after tests were failing) is exactly the regression kind-gating exists to catch, and
        # resetting on every swing would launder an elevated counter back to 0. So on a kind change
        # only the recorded (count, kind) baseline updates.
        digest = check_digest.digest_checks(failing, project_dir=project_dir)
        failures_now = digest.error_count or 1
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
            "[debugging] run=%s | round %s: %s failing [%s] across %d file(s) (was %s%s) — %s, "
            "stall counter now %s",
            state.get("run_id") or "-",
            state["debug_rounds"],
            failures_now,
            check_name,
            len(digest.errors_by_file),
            "n/a" if first_round else failures_before,
            "" if first_round else f" [{kind_before}]",
            "kind changed (neutral)" if kind_changed else ("progress" if made_progress else "NO progress"),
            state["debug_attempt"],
        )

        system = self._load_prompt("debugging")
        unresolved = list(state.get("unresolved_imports", []))
        prompt = self._build_prompt(check_name, failing, digest, manifest, unresolved)
        touched: list[str] = []          # workspace paths written by write_file/edit_file, live
        max_iters = self._max_iters_for(len(digest.errors_by_file))

        raw = ""
        llm_error = ""
        try:
            raw = self.llm.complete_with_tools(
                prompt=prompt, system=system, tools=self._tools(executor, project_dir, touched),
                max_iters=max_iters,
            )
        except Exception as exc:  # noqa: BLE001 - a provider/network failure ends THIS round, not the run
            llm_error = f"{type(exc).__name__}: {exc}"
            logger.exception("[debugging] run=%s | the fix session's LLM call failed", state.get("run_id") or "-")
        stop_reason = getattr(self.llm, "last_stop_reason", None)

        # Legacy / fallback: a final reply that IS a JSON fix proposal is still applied.
        fixes, parse_error = _parse_files(raw) if raw else (None, "no reply")
        if fixes is None and not touched and not llm_error:
            # The session changed nothing through the tools and its reply carried no fix — e.g. the
            # model spent its budget exploring. ONE tool-free retry asks for the fix as JSON (one
            # cheap call instead of accepting an empty round). Matches the parse-failure retry
            # unit_test.py / code_generator.py use.
            retry_prompt = (
                f"{prompt}\n\nYou did not change any file (no write_file/edit_file call), and your "
                "reply was not valid JSON matching "
                f'{{"files":[{{"path":...,"content":...}}],"notes":...}}. Error: {parse_error}. '
                "Reply now with STRICT JSON only — no prose, no code fences — carrying the "
                "COMPLETE corrected content of each file you change."
            )
            try:
                retry_raw = self.llm.complete(prompt=retry_prompt, system=system)
                fixes, parse_error = _parse_files(retry_raw)
                if fixes is not None or not raw:
                    raw = retry_raw
                stop_reason = getattr(self.llm, "last_stop_reason", stop_reason)
            except Exception as exc:  # noqa: BLE001
                llm_error = f"{type(exc).__name__}: {exc}"
                logger.exception("[debugging] run=%s | the tool-free retry failed", state.get("run_id") or "-")

        written_paths: list[str] = list(touched)
        if fixes:
            # Write under the SAME <project_dir>/ prefix the code_generator used (and that the
            # completeness gate checks). Without this the fix writes a bare path the gate never
            # looks for, so the missing file stays "missing" and the loop burns to the cap.
            for entry in fixes:
                path = _project_path(project_dir, entry["path"])
                try:
                    executor.write_file(path, entry["content"])
                except Exception:  # noqa: BLE001 - one bad model-proposed path (e.g. a traversal
                    # path LocalDiskExecutor._resolve rejects) must not sink every OTHER legitimate
                    # fix in this round; skip just this file and keep going.
                    logger.exception(
                        "debugging: failed to write fix %r for run %s — skipping this file, "
                        "continuing with the rest of the round",
                        path, state.get("run_id"),
                    )
                    continue
                if path not in written_paths:
                    written_paths.append(path)

        if written_paths:
            generated = list(state.get("generated_code", []))
            generated.extend(p for p in written_paths if p not in generated)
            state["generated_code"] = generated
            logger.info(
                "[debugging] run=%s | round %s: wrote %d fixed file(s): %s",
                state.get("run_id") or "-", state["debug_rounds"], len(written_paths), ", ".join(written_paths),
            )
        else:
            logger.warning(
                "debugging: round %s for run %s changed no file (%s)",
                state["debug_rounds"], state.get("run_id"),
                llm_error or f"stop reason {stop_reason or 'n/a'}; {parse_error}",
            )

        if unresolved:
            # Re-check the reconcile pass's findings against what THIS round changed, so a later
            # round isn't handed the same "fix this" list for an import that was already fixed —
            # see _prune_unresolved for exactly what counts as resolved.
            state["unresolved_imports"] = _prune_unresolved(
                unresolved, project_dir=project_dir,
                generated_code=state.get("generated_code", []),
                fixes=self._written_contents(executor, project_dir, written_paths),
            )
        self._record_round(
            state,
            check_name=check_name,
            failures_now=failures_now,
            failures_before=failures_before,
            made_progress=made_progress,
            written_paths=written_paths,
            notes=_parse_notes(raw),
            failing=failing,
            digest=digest,
            max_iters=max_iters,
            stop_reason=stop_reason,
            llm_error=llm_error,
            parse_error="" if written_paths else parse_error,
        )
        # NO git_commit, NO check here — the graph routes back to the fixed check.
        return state

    @staticmethod
    def _written_contents(executor: Executor, project_dir: str, written_paths: list[str]) -> list[dict[str, str]]:
        """``[{path, content}]`` (project-relative) for what this round wrote, for import pruning."""
        prefix = f"{project_dir}/"
        out: list[dict[str, str]] = []
        for path in written_paths:
            try:
                content = executor.read_file(path)
            except Exception:  # noqa: BLE001 - pruning is best-effort; an unreadable file stays listed
                continue
            out.append({"path": path[len(prefix):] if path.startswith(prefix) else path, "content": content})
        return out

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
        failing: list[dict[str, Any]] | None = None,
        digest: check_digest.CheckDigest | None = None,
        max_iters: int = DEBUG_MAX_ITERS,
        stop_reason: str | None = None,
        llm_error: str = "",
        parse_error: str = "",
    ) -> None:
        """Append this round to ``generation_summary`` and the run's debugging report.

        The report must answer, after the fact, the questions a stuck loop raises: what was failing
        and where, whether the round helped, what it changed (TEST edits flagged), and — when it
        changed nothing — WHY (the LLM call failed, the reply was cut off at ``max_tokens``, the
        failure was environmental). Written to disk every round so a crashed or killed run still
        leaves the rounds it completed. Mirrors the report shape of Code Review / Refactoring /
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
            + (" — LLM call failed" if llm_error else "")
        )
        state["generation_summary"] = (state.get("generation_summary") or "") + headline + "\n"
        # Always reaches the live UI log, even past graph_server's per-stage line cap (a 16+-turn
        # tool loop alone exceeds it, which used to hide exactly this closing line).
        logger.info(headline, extra={"ui_always": True})

        checks_cell = ", ".join(str(c.get("name", "?")) for c in (failing or [])) or check_name
        rows = "\n".join(f"| `{p}` | {'TEST' if _is_test_path(p) else 'source'} |" for p in written_paths)
        if written_paths:
            changed = f"**Files changed ({len(written_paths)}):**\n\n| File | Kind |\n| --- | --- |\n{rows}\n"
        elif llm_error:
            changed = f"**Files changed:** none — the LLM call failed: `{llm_error}`.\n"
        else:
            changed = (
                "**Files changed:** none — the model changed no file and its reply carried no fix "
                f"(stop reason: `{stop_reason or 'n/a'}`"
                + (f"; {parse_error}" if parse_error else "")
                + ").\n"
            )
        worst = ""
        if digest is not None and digest.errors_by_file:
            listed = "\n".join(f"| {n} | `{p}` |" for p, n in digest.top_files(10))
            worst = f"\n**Files with the most errors:**\n\n| Errors | File |\n| --- | --- |\n{listed}\n"
        environment = ""
        if digest is not None and digest.environment:
            environment = (
                "\n**Looks environmental (no code edit can fix these):**\n\n"
                + "\n".join(f"- {s}" for s in digest.environment) + "\n"
            )
        section = (
            f"\n## Round {round_no} — {check_name}\n\n"
            f"| Field | Value |\n| --- | --- |\n"
            f"| Failing checks | {checks_cell} |\n"
            f"| Failing before | {'(first measured round)' if failures_before < 0 else failures_before} |\n"
            f"| Failing now | {failures_now} |\n"
            f"| Files with errors | {len(digest.errors_by_file) if digest else 'n/a'} |\n"
            f"| Progress | {'yes' if made_progress else 'NO'} |\n"
            f"| Stall counter | {state.get('debug_attempt', 0)} |\n"
            f"| Tool budget | {max_iters} turns |\n"
            f"| Stop reason | {stop_reason or 'n/a'} |\n\n"
            + changed
            + worst
            + environment
            + (f"\n**Agent notes:** {notes}\n" if notes else "")
        )
        report = (state.get("debugging_report") or _DEBUG_REPORT_HEADER) + section
        state["debugging_report"] = report

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
        failing: list[dict[str, Any]],
        digest: check_digest.CheckDigest,
        manifest: list[str],
        unresolved_imports: list[dict[str, Any]] | None = None,
    ) -> str:
        """Point the model at the WHOLE failure + what exists, and let it pull content on demand.

        The file list is a map of what is readable, not inlined content: the model reads exactly
        the files the failure implicates. Every failing check's output is included once (stdout
        only where stderr does not already carry it), led by the errors-by-file table so the worst
        files are fixed first, and by any environmental signal so the model does not rewrite code
        to "fix" a missing compiler.

        ``unresolved_imports`` (plain dicts — see ``UnresolvedImport.to_dict()``) is included
        because a bundler reports only the FIRST unresolvable import and then stops: fixing it just
        reveals the next one. Handing over the whole pre-computed list lets one round fix all of
        them. Capped at ``_MAX_UNRESOLVED_IN_PROMPT`` entries.
        """
        parts: list[str] = [
            f"The fixed {check_name} check failed: {len(failing)} failing check(s), "
            f"{digest.error_count} error(s) counted across {len(digest.errors_by_file)} file(s)."
        ]

        if digest.environment:
            parts.append(
                "These failures look ENVIRONMENTAL — they come from the build host, not from the "
                "generated code, and editing source files will not fix them:\n"
                + "\n".join(f"- {s}" for s in digest.environment)
                + "\nDo not rewrite unrelated code to work around them. Fix what IS wrong in the "
                "code, and name these in your summary."
            )

        if digest.errors_by_file:
            top = digest.top_files(_TOP_FILES_IN_PROMPT)
            table = "\n".join(f"| {n} | {p} |" for p, n in top)
            more = len(digest.errors_by_file) - len(top)
            parts.append(
                "Errors by file, worst first. Fix from the TOP of this list; aim for about "
                f"{_FILES_PER_ROUND_HINT} files this round — the check re-runs after you and "
                "whatever remains comes back next round:\n"
                f"| errors | file |\n| --- | --- |\n{table}"
                + (f"\n(+{more} more file(s) with errors)" if more > 0 else "")
            )

        for check in failing:
            text = cap_output(check_digest.check_output(check))
            parts.append(f"Output of the failing `{check.get('name', '?')}` check:\n{text or '(no output)'}")

        if unresolved_imports:
            shown = unresolved_imports[:_MAX_UNRESOLVED_IN_PROMPT]
            listed = "\n".join(f"- {_render_unresolved(item)}" for item in shown)
            overflow = len(unresolved_imports) - len(shown)
            if overflow > 0:
                listed += f"\n- ... and {overflow} more (fix these first; the rest resurface next round)"
            parts.append(
                f"A deterministic pre-pass found {len(unresolved_imports)} relative import(s) that "
                "resolve to NO generated file. The output above may only report the first one; "
                "these are very likely the same root cause — either create the missing module or "
                "correct the importer, whichever matches the conventions the rest of the project "
                f"already uses:\n{listed}"
            )

        files_list = "\n".join(f"- {path}" for path in manifest) or "(none on record)"
        parts.append(
            "Generated file(s) in this project (call read_file on whichever ones the failure "
            f"implicates — do not assume you need all of them):\n{files_list}"
        )
        parts.append(
            "Apply each fix with edit_file (an exact snippet replacement — preferred) or write_file "
            "(the COMPLETE new content). Every change is saved the moment you make it. When you "
            "are done, reply with a short plain-text summary of which files you changed and why — "
            "if you changed a TEST file, say why the test rather than the source was wrong. Do not "
            "paste file contents into that summary."
        )
        return "\n\n".join(parts)


def _current_failure(state: WorkflowState) -> tuple[str, str, str]:
    """Pick the freshest failure signal: (check_name, stderr, stdout) of its FIRST failing check.

    ``debug_result`` is always current: ``debug_check_node`` overwrites it every time it runs.
    ``test_result`` is NOT always current: only ``unit_test_run_node`` writes it, so it can still
    hold a stale failure from an earlier loop iteration after a later fix changes what actually
    fails. A failing ``debug_result`` is therefore always the live signal when present.
    The agent itself uses :func:`_failing_checks` (every failing check); this single-check view is
    kept for callers that want one representative failure.
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
    """(stderr, stdout) of the first failing check."""
    for check in gate_result.get("checks", []):
        if _code_failure(check):
            return str(check.get("stderr", "")), str(check.get("stdout", ""))
    return "", ""


def _parse_files(raw: str) -> tuple[list[dict[str, str]] | None, str]:
    """Parse a JSON fix proposal into a list of {path, content}. Returns (files, error) — the
    error string feeds the tool-free retry prompt so the re-ask names the SPECIFIC thing wrong."""
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
