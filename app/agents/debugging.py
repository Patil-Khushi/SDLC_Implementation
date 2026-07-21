"""Debugging Agent (LLM + tools) — the LLM-fix half of the post-commit debug/test loop.

Entered ONLY on a post-commit fixed-check failure: either the compile/build check
(``debug_result``) or the test suite (``test_result``). Per CLAUDE.md: the LLM proposes the fix
*content*; it may inspect the workspace via the repair tools (read-only git + install), but it
never executes the check and never commits. The repair tools are bound to the model THROUGH
``self.llm`` (``complete_with_tools``) so this module imports no provider SDK. Proposed file
content is then written back through the injected executor (fixed code disposes).

This node increments the LOCAL ``debug_attempt`` counter and never touches ``repair_attempt`` or
the orchestrator's ``attempt``. Entered on a debug/test check failure; its job is to propose
corrected file content for the failure signal in state.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.base import BaseAgent
from app.agents.code_generator import _extract_json, _project_dir, _project_path
from app.graph.state import WorkflowState
from app.integrations.executor import Executor, get_executor
from app.services.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)


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
        # LOCAL debug counter (NOT the same as repair_attempt or the orchestrator's attempt).
        state["debug_attempt"] = int(state.get("debug_attempt", 0)) + 1

        executor = self._resolve_executor()
        # The debug path is entered only on a post-commit check failure: propose corrected file
        # content for the failing check's stderr. debug_result is always fresh (debug_check_node
        # overwrites it every run); test_result can be stale (only unit_test_run_node writes it),
        # so a failing debug_result always wins - see _current_failure.
        check_name, stderr = _current_failure(state)
        current = self._read_current_files(executor, state)

        system = self._load_prompt("debugging")
        prompt = self._build_prompt(check_name, stderr, current)
        # Tools are bound to the model inside the gateway; the model may inspect/install/diff.
        raw = self.llm.complete_with_tools(prompt=prompt, system=system, tools=executor.get_repair_tools())

        fixes = _parse_files(raw)
        if fixes:
            # Write under the SAME <project_dir>/ prefix the code_generator used (and that the
            # completeness gate checks). Without this the fix writes a bare path the gate never
            # looks for, so the missing file stays "missing" and the loop burns to the cap.
            project_dir = _project_dir(state)
            generated = list(state.get("generated_code", []))
            for entry in fixes:
                path = _project_path(project_dir, entry["path"])
                executor.write_file(path, entry["content"])  # fixed code writes the proposal
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
        # NO git_commit, NO check here — the graph routes back to the fixed check.
        return state

    def _read_current_files(self, executor: Executor, state: WorkflowState) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in state.get("generated_code", []):
            try:
                out[path] = executor.read_file(path)
            except Exception:  # noqa: BLE001 - a missing file just means less context for the LLM
                continue
        return out

    @staticmethod
    def _build_prompt(check_name: str, stderr: str, current: dict[str, str]) -> str:
        files_block = "\n\n".join(f"### {path}\n{content}" for path, content in current.items()) or "(none on record)"
        return (
            f"The fixed {check_name} check failed. Captured stderr:\n{stderr or '(none)'}\n\n"
            f"Current generated file(s):\n{files_block}\n\n"
            'Return the corrected file(s) as STRICT JSON: {"files":[{"path":...,"content":...}],"notes":...}'
        )


def _current_failure(state: WorkflowState) -> tuple[str, str]:
    """Pick the freshest failure signal.

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
        return "compile/build", _first_failure_stderr(debug_result)
    test_result = state.get("test_result")
    if test_result and not test_result.get("passed", True):
        return "test", _first_failure_stderr(test_result)
    return "unknown", ""


def _first_failure_stderr(gate_result: Any) -> str:
    for check in gate_result.get("checks", []):
        if not check.get("passed", True):
            return str(check.get("stderr", ""))
    return ""


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
    return _debugging_agent.execute(state)
