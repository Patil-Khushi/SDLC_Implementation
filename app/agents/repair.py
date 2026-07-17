"""Repair Agent (LLM + tools) — the repair path of the hybrid gate loop.

Entered ONLY on a gate failure. Per CLAUDE.md: the LLM proposes the fix *content*; it may
inspect the workspace via the repair tools (read-only git + install), but it never executes the
gate and never commits. The repair tools are bound to the model THROUGH ``self.llm``
(``complete_with_tools``) so this module imports no provider SDK. Proposed file content is then
written back through the injected executor (fixed code disposes).

This node increments the LOCAL ``repair_attempt`` counter and never touches the orchestrator's
``attempt``. Entered on a gate failure; its job is to propose corrected file content for the
failure signal in state.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.base import BaseAgent
from app.agents.code_generator import _extract_json
from app.graph.state import WorkflowState
from app.integrations.executor import Executor, get_executor
from app.services.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)


class RepairAgent(BaseAgent):
    name = "repair"

    def __init__(self, executor: Executor | None = None, llm: LLMGateway | None = None) -> None:
        super().__init__()
        if llm is not None:
            self.llm = llm
        self._executor = executor

    def _resolve_executor(self) -> Executor:
        return self._executor if self._executor is not None else get_executor()

    def execute(self, state: WorkflowState) -> WorkflowState:
        # LOCAL repair counter (reset per work item by the graph); never touches `attempt`.
        state["repair_attempt"] = int(state.get("repair_attempt", 0)) + 1

        executor = self._resolve_executor()
        # The repair path is entered only on a gate failure: propose corrected file content for
        # the failing check's stderr.
        stderr = _first_failure_stderr(state.get("gate_result") or {})
        current = self._read_current_files(executor, state)

        system = self._load_prompt("repair")
        prompt = self._build_prompt(stderr, current)
        # Tools are bound to the model inside the gateway; the model may inspect/install/diff.
        raw = self.llm.complete_with_tools(prompt=prompt, system=system, tools=executor.get_repair_tools())

        fixes = _parse_files(raw)
        if fixes:
            for entry in fixes:
                executor.write_file(entry["path"], entry["content"])  # fixed code writes the proposal
        else:
            # Proposal didn't parse: write nothing (no partial garbage). The gate re-runs and
            # will re-fail/escalate; log it so the no-op repair is debuggable.
            logger.warning(
                "repair: no valid fix parsed for run %s (attempt %s) — wrote nothing",
                state.get("run_id"),
                state.get("repair_attempt"),
            )
        # NO git_commit, NO gate here — the graph routes back to the fixed gate.
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
    def _build_prompt(stderr: str, current: dict[str, str]) -> str:
        files_block = "\n\n".join(f"### {path}\n{content}" for path, content in current.items()) or "(none on record)"
        return (
            f"The fixed quality gate failed. Captured stderr:\n{stderr or '(none)'}\n\n"
            f"Current generated file(s):\n{files_block}\n\n"
            'Return the corrected file(s) as STRICT JSON: {"files":[{"path":...,"content":...}],"notes":...}'
        )


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
_repair_agent = RepairAgent()


def repair_node(state: WorkflowState) -> WorkflowState:
    return _repair_agent.execute(state)
