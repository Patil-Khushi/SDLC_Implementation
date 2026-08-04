"""Unit Test Agent (post-Debugging phase).

Structurally a close twin of ``CodeGeneratorAgent`` (app/agents/code_generator.py): same
per-item prompt-building / JSON-parsing-with-retry-once / file-writing shape, but it writes
TEST files for the already-generated, already-committed source of every work item — once, after
Debugging's compile/build check has passed. Single responsibility (CLAUDE.md): write tests; no
gate/compile logic, no git, no routing.

Rules honored here:
- ``self.llm`` (the gateway) is the ONLY model access — no provider SDK import.
- All reads/writes go through the injected ``Executor`` — never open files or shell out directly.
- Writes only the fields this agent owns: ``unit_tests``, ``tests_ok``, ``generation_summary``,
  its own ``generation_metrics`` key (``tests_written``), and its own report pair
  (``unit_test_report`` / ``unit_test_report_path``). It never touches
  files_produced/seconds_per_item/compile_passes/compile_failures/repairs_used, and echoes
  run_id/attempt unchanged.
- One work item's generation failing to parse does NOT abort the run — partial test coverage is
  acceptable; only zero test files written WHILE work items existed makes ``tests_ok`` False. An
  empty plan (no work items at all — nothing to test) is trivially ``tests_ok`` True, not a failure.
  That success condition is a deliberate, tested invariant (see ``test_partial_failure_still_yields_
  second_items_success``) and is untouched here; coverage visibility below is purely additive.
- Before calling the model, an item with NO readable source file is skipped outright (never asked
  to invent tests for code it never saw) and an item's source block is size-capped before being
  inlined (this agent has no tools to fetch content on demand, unlike Debugging, so a budget on
  what it inlines is its only lever).
- The actual test runner (pytest / Jest / Vitest) is read as a FACT from the work item's root
  package.json rather than guessed from file shape — see ``_detect_test_runner``.
- Does not call ``executor.test()`` and does not set ``workflow_status`` — that is the fixed
  unit_test_run node's job (a separate step wires the node in app/graph/nodes.py).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.agents.base import BaseAgent
from app.agents.code_generator import _extract_json, _project_dir, _project_path
from app.config.settings import get_settings
from app.graph.state import WorkflowState
from app.integrations.executor import Executor, get_executor
from app.models import WorkItem
from app.services.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)

#: Char budget for the assembled ``files_block`` (ALL readable sources concatenated), mirroring
#: ``app.integrations.executor.cap_output`` (same order of magnitude as its ``OUTPUT_CAP``) — a
#: separate, smaller-scoped cap of our own rather than a direct reuse, since this bounds a PROMPT's
#: inlined source, not captured subprocess output. Applied to the WHOLE block, not per file —
#: inlining is this agent's only option (no tools to fetch content on demand), so an uncapped block
#: on a large work item's already-generated files can blow well past the model's context silently.
_PROMPT_SOURCE_CAP = 40_000
_PROMPT_HEAD_KEEP = 32_000
_PROMPT_TAIL_KEEP = 6_000

_RUNNER_LABELS = {"pytest": "pytest", "jest": "Jest", "vitest": "Vitest"}

_UNIT_TEST_REPORT_HEADER = (
    "# Unit Test Report\n\n"
    "One row per work item: whether it got a test file, how many, and the resolved outcome. A run\n"
    "can finish with `tests_ok=True` on a single passing item out of dozens — this\n"
    "report, not just that flag, is where the real coverage picture lives.\n"
)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value) or "run"


def _work_item_root(work_item: WorkItem) -> str:
    """The ``"backend/"`` or ``"frontend/"`` prefix this item's target files live under, or ``""``
    for a combined/legacy-shaped project with no such prefix.

    Checking only the FIRST target file is enough: this pipeline never nests a project root any
    deeper than one of these two canonical folders (``app.services.plan_builder``'s adaptive path
    re-roots every generated leaf under exactly one of them via ``_reroot``), and a work item's
    files never straddle both roots.
    """
    first = next((p for p in work_item.target_files if p), "")
    for root in ("backend/", "frontend/"):
        if first.startswith(root):
            return root
    return ""


def _is_python_item(work_item: WorkItem) -> bool:
    first = next((p for p in work_item.target_files if p), "")
    return first.endswith(".py")


def _runner_fact_line(runner: str) -> str:
    """State the resolved runner as a fact for the prompt (see ``_detect_test_runner``) instead of
    leaving the model to infer it from file shape — that inference can be flatly wrong (e.g. a
    Node/Express file tested with Vitest just because it lacks a React import)."""
    label = _RUNNER_LABELS.get(runner, "")
    if label:
        return f"This project's test runner is {label}. Write {label}-dialect tests only.\n"
    return (
        "This project's test runner could not be determined from its package.json (unreadable, "
        "or it declares neither jest nor vitest); use the ecosystem's most common convention for "
        "the source language shown below.\n"
    )


def _cap_files_block(text: str) -> str:
    """Head+tail-preserved truncation for the assembled source block — mirrors
    ``app.integrations.executor.cap_output``'s shape so a truncation marker and the trailing
    content both survive, rather than a naive head-only cut that silently drops whatever sits at
    the end."""
    if len(text) <= _PROMPT_SOURCE_CAP:
        return text
    dropped = len(text) - _PROMPT_HEAD_KEEP - _PROMPT_TAIL_KEEP
    return (
        text[:_PROMPT_HEAD_KEEP]
        + f"\n\n... [truncated {dropped} chars — output was {len(text)} chars total] ...\n\n"
        + text[-_PROMPT_TAIL_KEEP:]
    )


class UnitTestAgent(BaseAgent):
    name = "unit_test"

    def __init__(self, executor: Executor | None = None, llm: LLMGateway | None = None) -> None:
        super().__init__()
        if llm is not None:  # allow test/DI override of the gateway singleton
            self.llm = llm
        self._executor = executor

    def _resolve_executor(self) -> Executor:
        return self._executor if self._executor is not None else get_executor()

    def execute(self, state: WorkflowState) -> WorkflowState:
        executor = self._resolve_executor()
        project_dir = _project_dir(state)
        system = self._load_prompt("unit_test")

        written = list(state.get("unit_tests", []))
        total_new = 0
        attempted = 0    # work items the model was actually asked about (readable source existed)
        succeeded = 0    # of those, how many yielded at least one test file
        skipped = 0      # work items with NO readable source — never sent to the model (U3)
        outcomes: list[tuple[str, str, int]] = []  # (work_item.id, outcome, test-file count) for the report

        for work_item in state.get("work_items", []) or []:
            sources = self._read_sources(executor, project_dir, work_item)
            if not sources:
                # Calling the model with nothing to test would only get it to hallucinate tests for
                # code it never saw; those invented tests get written/committed and waste debug-loop
                # rounds reconciling against source that doesn't match. Kept OUT of `attempted` below
                # so a batch of genuinely-unreadable items doesn't make the coverage ratio look like
                # a model failure when the model was never even asked.
                skipped += 1
                outcomes.append((work_item.id, "skipped", 0))
                self._append_summary(
                    state, f"[unit_test] {work_item.id}: SKIPPED - no readable source files"
                )
                logger.info(
                    "[unit_test] run=%s | [SKIPPED] %s - no readable source files",
                    state.get("run_id") or "-",
                    work_item.id,
                )
                continue

            attempted += 1
            runner = self._detect_test_runner(executor, project_dir, work_item)
            files = self._generate_tests(work_item, sources, system, runner)

            if files is None:
                outcomes.append((work_item.id, "failed", 0))
                self._append_summary(
                    state,
                    f"[unit_test] {work_item.id}: FAILED - model did not return valid JSON (0 test files)",
                )
                logger.warning(
                    "[unit_test] run=%s | [FAILED] %s - model did not return valid JSON (0 test files)",
                    state.get("run_id") or "-",
                    work_item.id,
                )
                continue

            succeeded += 1
            new_paths = self._write_files(executor, project_dir, files)
            newly_added = 0
            for path in new_paths:
                if path not in written:
                    written.append(path)
                    newly_added += 1  # only count a path once toward the run-level metric (U6):
                    # `new_paths` can repeat a path the model already returned for an earlier item,
                    # and counting every occurrence would overcount tests_written relative to the
                    # de-duplicated `unit_tests` list.
            total_new += newly_added
            outcomes.append((work_item.id, "written", len(new_paths)))
            self._append_summary(state, f"[unit_test] {work_item.id}: {len(new_paths)} test file(s) written")
            logger.info(
                "[unit_test] run=%s | [DONE] %s - %d test file(s): %s",
                state.get("run_id") or "-",
                work_item.id,
                len(new_paths),
                ", ".join(new_paths) or "(none)",
            )

        state["unit_tests"] = written
        # False only when there WERE work items but none yielded a test file — an empty plan has
        # nothing to test and must not be misrouted to escalate (no-human-in-the-loop invariant).
        state["tests_ok"] = bool(written) or not (state.get("work_items") or [])
        self._bump_metrics(state, files=total_new)
        self._record_coverage(state, attempted=attempted, succeeded=succeeded, skipped=skipped, outcomes=outcomes)
        return state

    # -- generation -----------------------------------------------------------

    def _generate_tests(
        self, work_item: WorkItem, sources: dict[str, str], system: str, runner: str
    ) -> list[dict[str, str]] | None:
        """Ask the model for the {"files":[...]} JSON; re-ask once on parse failure."""
        prompt = self._build_prompt(work_item, sources, runner)
        parsed, error = self._parse(self.llm.complete(prompt=prompt, system=system))
        if parsed is None:
            retry = (
                f"{prompt}\n\nYour previous reply was not valid JSON matching "
                f'{{"files":[{{"path":...,"content":...}}]}}. Error: {error}. '
                "Reply with STRICT JSON only — no prose, no code fences."
            )
            parsed, error = self._parse(self.llm.complete(prompt=retry, system=system))
        return parsed

    @staticmethod
    def _build_prompt(work_item: WorkItem, sources: dict[str, str], runner: str) -> str:
        joined = "\n\n".join(f"### {path}\n{content}" for path, content in sources.items())
        files_block = _cap_files_block(joined) if joined else "(no source files could be read)"
        return (
            f"Work item: {work_item.id}\n"
            f"{_runner_fact_line(runner)}"
            f"Source file(s) to test:\n{files_block}\n\n"
            'Respond with STRICT JSON only: {"files":[{"path":...,"content":...}],"notes":...}'
        )

    @staticmethod
    def _parse(raw: str) -> tuple[list[dict[str, str]] | None, str]:
        """Parse the model reply into a list of {path, content}. Returns (files, error)."""
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

    # -- reading + writing ------------------------------------------------------

    def _read_sources(self, executor: Executor, project_dir: str, work_item: WorkItem) -> dict[str, str]:
        """Read the work item's already-generated target files for context. An unreadable file
        just means less context for the model — it is skipped, never a hard failure."""
        sources: dict[str, str] = {}
        for rel in work_item.target_files:
            path = _project_path(project_dir, rel)
            try:
                sources[path] = executor.read_file(path)
            except Exception:  # noqa: BLE001 - unreadable just means less context, not a failure
                continue
        return sources

    def _detect_test_runner(self, executor: Executor, project_dir: str, work_item: WorkItem) -> str:
        """The ACTUAL test runner this work item's root uses, read from that root's package.json.

        A project's manifest already states this as a fact — ``"jest"`` vs ``"vitest"`` in
        ``devDependencies``, or the ``scripts.test`` command — so there is no need to guess it from
        whether a file imports React. Python items skip this lookup entirely: pytest is the
        unambiguous, sole runner for ``.py`` sources and there is no package.json to read.

        Returns ``"pytest"``, ``"jest"``, ``"vitest"``, or ``""`` when undetermined (manifest
        missing/unreadable, or it declares neither runner) — ``_runner_fact_line`` handles that
        case with an honest "could not be determined" statement rather than a fabricated guess.
        """
        if _is_python_item(work_item):
            return "pytest"
        root = _work_item_root(work_item)
        manifest_path = _project_path(project_dir, f"{root}package.json")
        try:
            raw = executor.read_file(manifest_path)
        except Exception:  # noqa: BLE001 - unreadable/absent manifest -> runner stays undetermined
            return ""
        try:
            manifest = json.loads(raw)
        except (ValueError, TypeError):
            return ""
        if not isinstance(manifest, dict):
            return ""
        dev_deps = manifest.get("devDependencies")
        dev_deps = dev_deps if isinstance(dev_deps, dict) else {}
        if "jest" in dev_deps:
            return "jest"
        if "vitest" in dev_deps:
            return "vitest"
        scripts = manifest.get("scripts")
        test_script = str((scripts or {}).get("test", "")) if isinstance(scripts, dict) else ""
        if "jest" in test_script:
            return "jest"
        if "vitest" in test_script:
            return "vitest"
        return ""

    def _write_files(self, executor: Executor, project_dir: str, files: list[dict[str, str]]) -> list[str]:
        written: list[str] = []
        for entry in files:
            path = _project_path(project_dir, entry["path"])
            executor.write_file(path, entry["content"])
            written.append(path)
        return written

    # -- recording --------------------------------------------------------------

    @staticmethod
    def _append_summary(state: WorkflowState, line: str) -> None:
        state["generation_summary"] = (state.get("generation_summary") or "") + line + "\n"

    @staticmethod
    def _bump_metrics(state: WorkflowState, *, files: int) -> None:
        # Own only tests_written. files_produced/seconds_per_item/compile_passes/... are other
        # agents' fields — untouched here.
        metrics: dict[str, Any] = dict(state.get("generation_metrics") or {})
        metrics["tests_written"] = int(metrics.get("tests_written", 0)) + files
        state["generation_metrics"] = metrics

    def _record_coverage(
        self,
        state: WorkflowState,
        *,
        attempted: int,
        succeeded: int,
        skipped: int,
        outcomes: list[tuple[str, str, int]],
    ) -> None:
        """Surface coverage — not just the pass/fail ``tests_ok`` flag — durably.

        ``tests_ok`` is True the moment ANY item yielded a test file (a deliberate, tested invariant
        — see the module docstring); that is correct for routing but useless for seeing whether a
        run tested 1 of 59 items or 58 of 59. This method is purely additive
        visibility: a headline ratio in ``generation_summary`` (grep-free, unlike counting FAILED
        lines) plus a durable per-item Markdown report, mirroring debugging.py's report-writing
        pattern (a ``reports/<project>/*.md`` file + a ``*_report_path`` state field). Skips
        entirely when there were no work items at all (nothing to summarize).
        """
        if attempted + skipped == 0:
            return
        pct = round(succeeded / attempted * 100) if attempted else 0
        headline = (
            f"[unit_test] coverage: {succeeded}/{attempted} work item(s) got tests ({pct}%)"
            + (f", {skipped} skipped (no readable source)" if skipped else "")
        )
        self._append_summary(state, headline)

        rows = "\n".join(f"| `{wid}` | {outcome} | {count} |" for wid, outcome, count in outcomes)
        report = (
            _UNIT_TEST_REPORT_HEADER
            + f"\n{headline}\n\n"
            + "| Work item | Outcome | Test files |\n| --- | --- | --- |\n"
            + rows
            + "\n"
        )
        state["unit_test_report"] = report

        try:
            run_dir = Path(get_settings().reports_dir) / _slug(
                state.get("project_id") or state.get("run_id") or "run"
            )
            run_dir.mkdir(parents=True, exist_ok=True)
            md_path = run_dir / "unit-test-report.md"
            md_path.write_text(report, encoding="utf-8")
            state["unit_test_report_path"] = str(md_path)
        except Exception:  # noqa: BLE001 - a reporting failure must never break the run's real output
            logger.exception("unit_test: failed to write the report for run %s", state.get("run_id"))
