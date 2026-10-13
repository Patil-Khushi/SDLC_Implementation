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
  (``unit_test_report`` / ``unit_test_report_path``, mirroring Code Review's/Security's report
  fields). It never touches files_produced/seconds_per_item/compile_passes/compile_failures/
  repairs_used, and echoes run_id/attempt unchanged.
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
from app.services import pack_context as pc
from app.services.llm_gateway import LLMGateway, complete_cached
from app.services.naming_contract import build_module_map, build_naming_contract_from_package
from app.services.pack_index import index_for

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


_CODE_EXTS = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_TEST_NAME_RE = re.compile(r"(^test_.*\.py$|_test\.py$|\.(?:test|spec)\.[cm]?[jt]sx?$)", re.IGNORECASE)
_TOOL_CONFIG_PREFIXES = ("vite.config", "vitest.config", "jest.config", "jest.setup", "babel.config",
                         "postcss.config", "tailwind.config", "eslint.config", ".eslintrc", "webpack.config")
_UNTESTED_DIRS = {"migrations", "alembic", "versions", "seeds", "seeders", "tests", "test", "__tests__", "test-utils"}
_RUNTIME_JS_RE = re.compile(r"\bfunction\b|=>|\bclass\s+\w|module\.exports|export\s+default\b|export\s+(?:async\s+)?function")


def _is_test_file(path: str) -> bool:
    return bool(_TEST_NAME_RE.search(path.replace("\\", "/").rsplit("/", 1)[-1]))


def _testable(path: str, content: str) -> bool:
    """Is there behaviour in this file to test? Not for a package marker, a type-only module, a
    tool config, a migration, an entry that only renders into the DOM, or a test itself — asking
    for tests of those produces tests of nothing (or of the framework)."""
    parts = path.replace("\\", "/").split("/")
    base = parts[-1].lower()
    if not base.endswith(_CODE_EXTS) or base.endswith(".d.ts") or base.startswith(_TOOL_CONFIG_PREFIXES):
        return False
    if _is_test_file(path) or any(p.lower() in _UNTESTED_DIRS for p in parts[:-1]):
        return False
    if base.endswith(".py"):
        return bool(re.search(r"^\s*(?:async\s+)?def\s|^\s*class\s", content, re.MULTILINE))
    if re.search(r"\bcreateRoot\(|ReactDOM\.render\(", content) and not re.search(r"export\s", content):
        return False
    return bool(_RUNTIME_JS_RE.search(content))


def _suggested_test_path(path: str, python_paths: list[str]) -> str:
    """Where this source file's tests go — unique per source file, so one call never overwrites
    another's. Python: ``<import root>/tests/test_<module path>.py`` (``app.modules.depots.service``
    -> ``backend/tests/test_modules_depots_service.py``), importable as ``app...`` because the
    runner starts from the backend folder. JS/TS: colocated ``<stem>.test.<ext>``."""
    from app.services.python_wiring import PyProject

    norm = path.replace("\\", "/")
    if norm.endswith(".py"):
        project = PyProject(dict.fromkeys(python_paths or [norm], ""))
        dotted = project.dotted_of(norm) or norm.rsplit("/", 1)[-1][:-3]
        roots = [r for r in project.roots_for(norm) if norm.startswith(r)]
        root = roots[0] if roots else (norm.split("/", 1)[0] + "/" if "/" in norm else "")
        parts = dotted.split(".")
        return f"{root}tests/test_{'_'.join(parts[1:] or parts)}.py"
    folder, _, base = norm.rpartition("/")
    stem, dot, ext = base.rpartition(".")
    return f"{folder + '/' if folder else ''}{stem}.test.{ext}" if dot else f"{norm}.test.js"


def _app_import(work_items: list[Any], side: str) -> tuple[str, str] | None:
    """``(module, variable)`` of the backend's FastAPI app (``app.main``, ``app``), from where the
    plan put its entry file."""
    from app.services.python_wiring import PyProject

    paths = [p for item in work_items or [] for p in (getattr(item, "target_files", None) or []) if p.endswith(".py")
             and p.startswith(side)]
    entry = next((p for p in paths if p.rsplit("/", 1)[-1] in ("main.py", "app.py", "asgi.py")), None)
    if not entry:
        return None
    dotted = PyProject(dict.fromkeys(paths, "")).dotted_of(entry)
    return (dotted, "app") if dotted else None


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
        run_id = state.get("run_id") or "-"

        written = list(state.get("unit_tests", []))
        total_new = 0
        items = state.get("work_items", []) or []
        total_items = len(items)
        attempted = 0    # work items the model was actually asked about (readable source existed)
        succeeded = 0    # of those, how many yielded at least one test file
        skipped = 0      # work items with NO readable source — never sent to the model (U3)
        untestable = 0   # readable, but no behaviour to test — never sent to the model either
        rows: list[dict[str, Any]] = []  # per-item outcome, for the report

        logger.info("[unit_test] run=%s | [1/%d] generating tests for %d work item(s)...",
                    run_id, total_items or 1, total_items)

        for i, work_item in enumerate(items, start=1):
            sources = self._read_sources(executor, project_dir, work_item)
            if not sources:
                # Calling the model with nothing to test would only get it to hallucinate tests for
                # code it never saw; those invented tests get written/committed and waste debug-loop
                # rounds reconciling against source that doesn't match. Kept OUT of `attempted` below
                # so a batch of genuinely-unreadable items doesn't make the coverage ratio look like
                # a model failure when the model was never even asked.
                skipped += 1
                rows.append({"id": work_item.id, "status": "SKIPPED", "files": []})
                self._append_summary(
                    state, f"[unit_test] {work_item.id}: SKIPPED - no readable source files"
                )
                logger.info(
                    "[unit_test] run=%s | [SKIPPED] %s - no readable source files",
                    run_id, work_item.id,
                )
                continue

            testable = [p for p, content in sources.items() if _testable(p, content)]
            if not testable:
                # Nothing to test (types, config, package markers, a DOM-mount entry) — not a
                # generation failure: unlike an unreadable item, it never counts against tests_ok.
                untestable += 1
                rows.append({"id": work_item.id, "status": "NOTHING TO TEST", "files": []})
                self._append_summary(
                    state, f"[unit_test] {work_item.id}: nothing to test (types, config, markers only)"
                )
                continue

            attempted += 1
            runner = self._detect_test_runner(executor, project_dir, work_item)
            logger.info(
                "[unit_test] run=%s | [%d/%d] %s: read %d source file(s), asking the model for tests of %d...",
                run_id, i, total_items, work_item.id, len(sources), len(testable),
            )
            # Shared by every item of the run (the contract, the module map), then by every file of
            # this item (its sources and pack slices): the gateway's cacheable prefix.
            prefix = [s for s in (self._shared_context(state, work_item),
                                  self._item_context(state, work_item, sources, runner, executor)) if s]
            python_paths = [p for item in items for p in item.target_files if p.endswith(".py")]
            files: list[dict[str, str]] | None = []
            for source in testable:
                rel = source[len(project_dir) + 1:] if source.startswith(f"{project_dir}/") else source
                produced = self._generate_test_file(rel, _suggested_test_path(rel, python_paths), prefix, system)
                if produced:
                    files.extend(produced)
            if not files:
                files = None

            if files is None:
                self._append_summary(
                    state,
                    f"[unit_test] {work_item.id}: FAILED - model did not return valid JSON (0 test files)",
                )
                logger.warning(
                    "[unit_test] run=%s | [%d/%d] [FAILED] %s - model did not return valid JSON (0 test files)",
                    run_id, i, total_items, work_item.id,
                )
                rows.append({"id": work_item.id, "status": "FAILED", "files": []})
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
            self._append_summary(state, f"[unit_test] {work_item.id}: {len(new_paths)} test file(s) written")
            logger.info(
                "[unit_test] run=%s | [%d/%d] [DONE] %s - %d test file(s): %s",
                run_id, i, total_items, work_item.id,
                len(new_paths),
                ", ".join(new_paths) or "(none)",
            )
            rows.append({"id": work_item.id, "status": "written", "files": new_paths})

        state["unit_tests"] = written
        # False only when there WERE work items but none yielded a test file — an empty plan has
        # nothing to test and must not be misrouted to escalate (no-human-in-the-loop invariant).
        # ...and an item with nothing testable is not one of those failures: a project whose every
        # item is types/config/markers had nothing to test, which is not "test generation failed".
        state["tests_ok"] = bool(written) or not (state.get("work_items") or []) or (attempted == 0 and skipped == 0)
        self._bump_metrics(state, files=total_new)
        self._write_report(state, run_id, rows, total_new, attempted=attempted, succeeded=succeeded, skipped=skipped)
        return state

    def _write_report(
        self,
        state: WorkflowState,
        run_id: str,
        rows: list[dict[str, Any]],
        total_new: int,
        *,
        attempted: int,
        succeeded: int,
        skipped: int,
    ) -> None:
        """Persist a Markdown report of what this run's test generation actually did.

        Runs once, at the end of the (single) node invocation — unlike Debugging's per-attempt
        accumulation, Unit Test Generation is not itself a retry loop, so the whole report is
        known by the time ``execute`` returns.
        """
        # Headline coverage ratio in generation_summary — grep-free, unlike counting FAILED/SKIPPED
        # lines by hand. Skipped entirely when there were no work items at all.
        if attempted + skipped:
            pct = round(succeeded / attempted * 100) if attempted else 0
            headline = (
                f"[unit_test] coverage: {succeeded}/{attempted} work item(s) got tests ({pct}%)"
                + (f", {skipped} skipped (no readable source)" if skipped else "")
            )
            self._append_summary(state, headline)

        project_id = state.get("project_id") or run_id or "project"
        report = _render_report(project_id, run_id, rows, total_new)
        state["unit_test_report"] = report

        try:
            run_dir = Path(get_settings().reports_dir) / _slug(project_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            path = run_dir / "unit-test-report.md"
            path.write_text(report, encoding="utf-8")
            state["unit_test_report_path"] = str(path)
            logger.info("[unit_test] run=%s | report saved to %s", run_id, path)
        except OSError:
            # Every real result (unit_tests/tests_ok/generation_metrics) is already finalized by
            # the time this runs — a report-persistence hiccup (a locked file, a full disk) must
            # not throw away that completed work by crashing the run.
            logger.exception("[unit_test] run=%s | could not persist the report", run_id)

    # -- generation -----------------------------------------------------------

    def _generate_test_file(
        self, source: str, test_path: str, prefix: list[str], system: str
    ) -> list[dict[str, str]] | None:
        """ONE test file for ONE source file; re-ask once on a parse failure. One file per reply,
        so a module with many files cannot truncate the reply at the output cap (the failure the
        code generator's per-file path exists for). The reply's own path is kept when it names a
        test file; otherwise the suggested one is used."""
        prompt = self._file_prompt(source, test_path)
        parsed, error = self._parse(complete_cached(self.llm, prompt, cache_prefix=prefix, system=system))
        if parsed is None:
            retry = (
                f"{prompt}\n\nYour previous reply was not valid JSON matching "
                f'{{"files":[{{"path":...,"content":...}}]}}. Error: {error}. '
                "Reply with STRICT JSON only — no prose, no code fences."
            )
            parsed, error = self._parse(complete_cached(self.llm, retry, cache_prefix=prefix, system=system))
        if parsed is None:
            return None
        tests = [f for f in parsed if _is_test_file(f["path"])]
        if not tests:
            tests = [{"path": test_path, "content": parsed[0]["content"]}]
        return tests

    @staticmethod
    def _file_prompt(source: str, test_path: str) -> str:
        return (
            f"Source file(s) to test: {source}  (its content is among this work item's sources above)\n"
            f"Write the test file for THIS source file only, at: {test_path}\n"
            'Respond with STRICT JSON only, containing exactly one file: '
            '{"files":[{"path":...,"content":...}],"notes":...}'
        )

    @staticmethod
    def _shared_context(state: WorkflowState, work_item: WorkItem) -> str:
        """The names and imports the tests must use — the naming contract and this side's module
        map. A test importing ``app.routers.auth`` or asserting on ``UserSchema`` when the code says
        ``UserOut`` fails for a reason that has nothing to do with the behaviour under test."""
        package = state.get("design_package") or {}
        try:
            blocks = [build_naming_contract_from_package(package),
                      build_module_map(state.get("work_items"), for_item=work_item)]
        except Exception:  # noqa: BLE001 - context is help, never a reason to lose the tests
            logger.exception("[unit_test] could not assemble the project context")
            return ""
        return "\n\n".join(b for b in blocks if b)

    def _item_context(
        self, state: WorkflowState, work_item: WorkItem, sources: dict[str, str], runner: str, executor: Executor
    ) -> str:
        """What THIS item's tests are written against: its sources, what they must do (requirement
        text), the exact messages and shapes to assert (validation rules, sample payloads), and the
        environment the tests run in."""
        joined = "\n\n".join(f"### {path}\n{content}" for path, content in sources.items())
        parts = [
            f"Work item: {work_item.id}",
            _runner_fact_line(runner).strip(),
            "Sources of this work item (the code under test — import it, never copy it):\n" + _cap_files_block(joined),
        ]
        try:
            index = index_for(state.get("design_package") or {})
            endpoints = list(work_item.endpoints) or pc.screen_endpoints(index, work_item.screens)
            ops = pc.operations_for(index, endpoints)
            sections = (
                ("## Requirements these files implement — test THESE behaviours",
                 pc.requirements_section(index, work_item.requirement_ids)),
                ("## Validation rules — assert these messages VERBATIM", pc.validation_section(index, ops, endpoints)),
                ("## Sample requests/responses from the design — use them as test data",
                 pc.payloads_section(index, ops)),
            )
            parts += [f"{title}\n{body}" for title, body in sections if body]
        except Exception:  # noqa: BLE001
            logger.exception("[unit_test] could not slice the design pack for %s", work_item.id)
        try:
            facts = executor.environment_facts()
        except Exception:  # noqa: BLE001
            facts = {}
        parts.append("## Test environment\n" + "\n".join(f"- {line}" for line in
                                                          self._environment_lines(state, work_item, sources, runner, facts)))
        return "\n\n".join(parts)

    @staticmethod
    def _environment_lines(
        state: WorkflowState, work_item: WorkItem, sources: dict[str, str], runner: str, facts: dict[str, str]
    ) -> list[str]:
        lines: list[str] = []
        side = _work_item_root(work_item)
        code = "\n".join(sources.values())
        if runner == "pytest":
            app = _app_import(state.get("work_items") or [], side)
            if "fastapi" in code.lower():
                where = f"`from {app[0]} import {app[1]}`" if app else "the FastAPI app module"
                lines.append(
                    f"HTTP behaviour: `from fastapi.testclient import TestClient` against {where}; replace "
                    "dependencies with `app.dependency_overrides[<dependency>] = <fake>` (the database session, "
                    "the current user and role checks) and clear them after each test.")
            lines.append("pytest, pytest-asyncio and httpx are installed. Tests run with `python -m pytest` from "
                         "the backend folder: import the project exactly as the MODULE MAP names it.")
        elif runner in ("jest", "vitest") and side == "frontend/":
            lines.append("Components: @testing-library/react on jsdom; jest-dom matchers are already set up; CSS and "
                         "asset imports are mapped to stubs. Mock the API module a component calls; never hit the "
                         "network.")
        elif runner in ("jest", "vitest"):
            lines.append("HTTP routes: supertest against the exported app (require the app module, never call "
                         "listen()); mock repository / database modules.")
        if facts.get("database"):
            lines.append(f"Database: {facts['database']}. Unit tests must not depend on it — isolate with the "
                         "overrides/mocks above; a test that does touch it creates and removes its own rows.")
        lines.append("Each reply is ONE self-contained test file: define its fixtures and mocks inside it. Never "
                     "write conftest.py, setup files or shared helpers — other calls write the other test files.")
        return lines

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

def _esc(text: str) -> str:
    """Escape text for a Markdown table cell (mirrors security.py/code_review.py's ``_esc``).

    Work-item ids and test-file paths land here from the model's own JSON reply (via
    ``_extract_json``, which tolerates raw control characters in string values) with no character
    sanitization anywhere upstream — a literal ``|`` would inject an extra table column, and a
    literal newline would split a row mid-table, corrupting the rest of the persisted report.
    """
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _render_report(project_id: str, run_id: str, rows: list[dict[str, Any]], total_new: int) -> str:
    failed_rows = [r for r in rows if r["status"] == "FAILED"]
    skipped_rows = [r for r in rows if r["status"] == "SKIPPED"]

    L: list[str] = []
    a = L.append
    a("# Unit Test Generation Report\n")

    a("## Metadata\n")
    a("| Field | Value |")
    a("| --- | --- |")
    a(f"| Project | {project_id} |")
    a(f"| Run ID | {run_id or '-'} |")
    a(f"| Work items | {len(rows)} |")
    a(f"| Test files written | {total_new} |")
    a(f"| Failed to generate | {len(failed_rows)} |")
    a(f"| Skipped (no readable source) | {len(skipped_rows)} |\n")

    a("## Per-item results\n")
    if rows:
        a("| Work item | Status | Test file(s) |")
        a("| --- | --- | --- |")
        for r in rows:
            files = ", ".join(f"`{_esc(p)}`" for p in r["files"]) or "-"
            a(f"| {_esc(r['id'])} | {r['status']} | {files} |")
        a("")
    else:
        a("_No work items — nothing to test._\n")

    if failed_rows:
        a("## Failures\n")
        for r in failed_rows:
            a(f"- **{_esc(r['id'])}**: the model did not return valid JSON after one retry (0 test files)")
        a("")

    return "\n".join(L)
