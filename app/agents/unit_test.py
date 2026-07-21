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
  and its own ``generation_metrics`` key (``tests_written``). It never touches
  files_produced/seconds_per_item/compile_passes/compile_failures/repairs_used, and echoes
  run_id/attempt unchanged.
- One work item's generation failing to parse does NOT abort the run — partial test coverage is
  acceptable; only zero test files written WHILE work items existed makes ``tests_ok`` False. An
  empty plan (no work items at all — nothing to test) is trivially ``tests_ok`` True, not a failure.
- Does not call ``executor.test()`` and does not set ``workflow_status`` — that is the fixed
  unit_test_run node's job (a separate step wires the node in app/graph/nodes.py).
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.base import BaseAgent
from app.agents.code_generator import _extract_json, _project_dir, _project_path
from app.graph.state import WorkflowState
from app.integrations.executor import Executor, get_executor
from app.models import WorkItem
from app.services.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)


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

        for work_item in state.get("work_items", []) or []:
            sources = self._read_sources(executor, project_dir, work_item)
            files = self._generate_tests(work_item, sources, system)

            if files is None:
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

            new_paths = self._write_files(executor, project_dir, files)
            for path in new_paths:
                if path not in written:
                    written.append(path)
            total_new += len(new_paths)
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
        return state

    # -- generation -----------------------------------------------------------

    def _generate_tests(
        self, work_item: WorkItem, sources: dict[str, str], system: str
    ) -> list[dict[str, str]] | None:
        """Ask the model for the {"files":[...]} JSON; re-ask once on parse failure."""
        prompt = self._build_prompt(work_item, sources)
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
    def _build_prompt(work_item: WorkItem, sources: dict[str, str]) -> str:
        files_block = (
            "\n\n".join(f"### {path}\n{content}" for path, content in sources.items())
            or "(no source files could be read)"
        )
        return (
            f"Work item: {work_item.id}\n"
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
