"""Refactoring Agent (LLM + tools) — applies the fixes the code review named.

Runs AFTER Code Review (its producer). It reads the structured findings the review
recorded (``review_findings_path`` — a JSON list; falls back to the JSON block the
Markdown ``review_report`` embeds), groups them per file, and asks the model —
THROUGH ``self.llm`` (so this module imports no provider SDK) — to return corrected
file content. Proposed content is written back through the injected executor under the
SAME ``<project_dir>/`` prefix the code generator / repair path use, so the next agent
(Debugging) sees the fixes on the shared exec-sandbox.

Mirrors ``repair.py`` (LLM proposes file content -> written back), but driven by the
review report rather than a gate failure. Per the team decision it does NOT commit,
push, or run any gate — verification belongs to the downstream agents.

Owns only: ``refactored_code`` (+ its ``workflow_status`` stamp). Reads ``review_report``
/ ``review_findings_path`` and the code via the executor.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.agents.base import BaseAgent
from app.agents.code_generator import _extract_json, _project_dir, _project_path
from app.graph.state import WorkflowState
from app.integrations.executor import Executor, get_executor
from app.services.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)

#: Fan-out cap: never fire more than this many per-file LLM repairs in one run. Overflow files are
#: reported (not silently dropped) so a huge review can't turn into an unbounded burst of calls.
MAX_FILES_PER_RUN = 25


class RefactoringAgent(BaseAgent):
    name = "refactoring"

    def __init__(self, executor: Executor | None = None, llm: LLMGateway | None = None) -> None:
        super().__init__()
        if llm is not None:  # allow test/DI override of the gateway singleton
            self.llm = llm
        self._executor = executor

    def _resolve_executor(self) -> Executor:
        return self._executor if self._executor is not None else get_executor()

    def execute(self, state: WorkflowState) -> WorkflowState:
        findings = self._load_findings(state)
        actionable = [f for f in findings if str(f.get("file", "")).strip()]
        if not actionable:
            # Nothing file-scoped to fix (clean review, or only project-level notes). Stamp the
            # status so the graph can route on, and record why nothing was written.
            state["refactored_code"] = "No file-scoped review findings to apply; nothing refactored."
            state["workflow_status"] = "refactored"
            logger.info("refactoring: no actionable findings for run %s", state.get("run_id"))
            return state

        executor = self._resolve_executor()
        project_dir = _project_dir(state)
        system = self._load_prompt("refactoring")

        by_file = _group_by_file(actionable)
        files = sorted(by_file)
        deferred = files[MAX_FILES_PER_RUN:]
        files = files[:MAX_FILES_PER_RUN]

        generated = list(state.get("generated_code", []))
        fixed_files: list[str] = []
        skipped: list[str] = []
        applied_findings = 0

        for rel in files:
            file_findings = by_file[rel]
            path = _project_path(project_dir, rel)
            try:
                current = executor.read_file(path)
            except Exception:  # noqa: BLE001 - a file named in the review but absent here is skipped, not fatal
                skipped.append(f"{rel} (not found in workspace)")
                continue

            prompt = self._build_prompt(rel, file_findings, current)
            # Tools are bound to the model inside the gateway; the model may inspect/diff (read-only).
            raw = self.llm.complete_with_tools(
                prompt=prompt, system=system, tools=executor.get_repair_tools()
            )
            fixes = _parse_files(raw)
            if not fixes:
                # Proposal didn't parse: write nothing (no partial garbage), leave the file as-is.
                skipped.append(f"{rel} (no valid fix parsed)")
                logger.warning(
                    "refactoring: no valid fix parsed for %s (run %s) — wrote nothing",
                    rel, state.get("run_id"),
                )
                continue

            for entry in fixes:
                # Write under the SAME <project_dir>/ prefix the code generator used, so the next
                # agent reads the fixed file where it expects it (mirrors repair.py).
                out_path = _project_path(project_dir, entry["path"])
                executor.write_file(out_path, entry["content"])
                if out_path not in generated:
                    generated.append(out_path)
            fixed_files.append(rel)
            applied_findings += len(file_findings)

        state["generated_code"] = generated
        state["refactored_code"] = _summary(fixed_files, applied_findings, skipped, deferred)
        state["workflow_status"] = "refactored"
        logger.info(
            "refactoring: fixed %d/%d file(s), applied %d finding(s), skipped %d, deferred %d (run %s)",
            len(fixed_files), len(by_file), applied_findings, len(skipped), len(deferred),
            state.get("run_id"),
        )
        return state

    # -- findings ------------------------------------------------------------

    def _load_findings(self, state: WorkflowState) -> list[dict[str, Any]]:
        """Prefer the structured JSON the review recorded; fall back to the report's JSON block."""
        path = (state.get("review_findings_path") or "").strip()
        if path:
            try:
                raw = json.loads(Path(path).read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    return [f for f in raw if isinstance(f, dict)]
            except (OSError, ValueError):
                logger.warning(
                    "refactoring: could not read findings JSON at %s; falling back to report text", path
                )
        return _findings_from_report(state.get("review_report") or "")

    # -- prompt --------------------------------------------------------------

    @staticmethod
    def _build_prompt(rel: str, findings: list[dict[str, Any]], current: str) -> str:
        lines = "\n".join(_finding_line(f) for f in findings) or "(no detail)"
        return (
            "The code review flagged the issues below in this file. Apply ONLY the fixes needed to "
            "resolve them; do not rewrite unrelated code, restyle, or change behavior beyond the "
            "findings.\n\n"
            f"File: {rel}\n\n"
            f"Findings:\n{lines}\n\n"
            f"Current content:\n{current}\n\n"
            'Return the corrected file as STRICT JSON: '
            '{"files":[{"path":"' + rel + '","content":"<full corrected file>"}],"notes":"<what changed>"}'
        )


def _finding_line(f: dict[str, Any]) -> str:
    severity = f.get("severity") or "?"
    line = f.get("line")
    loc = f" line {line}" if line not in (None, 0, "") else ""
    tag = " ".join(str(x) for x in (f.get("category"), f.get("rule_id")) if x).strip()
    tag = f" ({tag})" if tag else ""
    msg = f.get("message") or f.get("tool_message") or ""
    return f"- [{severity}]{loc}{tag}: {msg}".rstrip()


def _group_by_file(findings: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        out.setdefault(str(f["file"]).strip(), []).append(f)
    return out


def _findings_from_report(report: str) -> list[dict[str, Any]]:
    """Fallback: the Markdown report embeds its actionable findings as a ```json fenced block.

    Scan every fenced JSON block, and return the first that parses to a list of finding dicts
    (each carrying a ``file``). Best-effort only — the structured ``review_findings_path`` is the
    primary source; this keeps refactoring working if only the report text is on the state.
    """
    for block in re.findall(r"```(?:json)?\s*\n(.*?)```", report, re.DOTALL):
        try:
            parsed = json.loads(block, strict=False)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, list) and any(isinstance(x, dict) and x.get("file") for x in parsed):
            return [x for x in parsed if isinstance(x, dict)]
    return []


def _parse_files(raw: str) -> list[dict[str, str]] | None:
    """Parse the model's ``{"files":[{"path","content"}]}`` reply (same shape as the repair path)."""
    obj = _extract_json(raw)
    if not isinstance(obj, dict) or not isinstance(obj.get("files"), list):
        return None
    clean: list[dict[str, str]] = []
    for entry in obj["files"]:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and isinstance(entry.get("content"), str):
            clean.append({"path": entry["path"], "content": entry["content"]})
    return clean or None


def _summary(fixed: list[str], applied: int, skipped: list[str], deferred: list[str]) -> str:
    parts = [f"Refactored {len(fixed)} file(s), applying {applied} review finding(s)."]
    if fixed:
        parts.append("Fixed: " + ", ".join(fixed))
    if skipped:
        parts.append("Skipped: " + ", ".join(skipped))
    if deferred:
        parts.append(f"Deferred (over {MAX_FILES_PER_RUN}-file cap): " + ", ".join(deferred))
    return " ".join(parts)


# Module-level agent reused across invocations (guide's node pattern). Executor + gateway are
# resolved at run time (provider / singleton), so tests inject via constructor / set_executor.
_refactoring_agent = RefactoringAgent()


def refactoring_node(state: WorkflowState) -> WorkflowState:
    return _refactoring_agent.execute(state)
