"""Refactoring Agent (LLM + tools) — applies the fixes the code review named.

Runs AFTER Code Review (its producer). It reads the structured findings the review recorded
(``review_findings_path`` — a JSON list), skips suppressed false positives, and then works like a
coding agent: it runs ONE agentic tool loop — THROUGH ``self.llm.complete_with_tools`` (so this
module imports no provider SDK) — where the model is given the findings plus ``read_file`` /
``write_file`` tools scoped to ``<project_dir>/`` and EDITS the flagged files directly, iterating
across them. The write tool records every touched path; edits land under the SAME
``<project_dir>/`` prefix the code generator / repair path use, so the next agent (Debugging) sees
the fixes on the shared exec-sandbox.

Unlike the old one-shot-per-file rewrite, the model drives: it reads each file, applies only the
fixes the findings call for, and moves on — no fixed JSON output contract. Per the team decision
it does NOT commit, push, or run any gate — verification belongs to the downstream agents.

Owns only: ``refactored_code`` (+ its ``workflow_status`` stamp). Reads ``review_report``
/ ``review_findings_path`` and the code via the executor.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.agents.base import BaseAgent
from app.agents.code_generator import _project_dir, _project_path
from app.graph.state import WorkflowState
from app.integrations.executor import Executor, RepairTool, get_executor
from app.services.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)

#: Cap on how many findings-bearing files one run hands to the model. Overflow files are reported
#: (not silently dropped) so a huge review can't turn into an unbounded edit session.
MAX_FILES_PER_RUN = 25

#: Tool-loop budget for the agentic edit session. Each iteration can read/write MANY files (the
#: gateway executes every tool call in a turn), so this is turns-of-reasoning, not files.
REFACTOR_MAX_ITERS = 16


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
        findings, load_error = self._load_findings(state)
        if load_error is not None:
            # The review's structured findings could not be loaded. Surface it as a real failure
            # instead of a silent "nothing to do": refactoring was skipped because its input was
            # missing/unreadable, NOT because the code was clean — a human needs to know.
            state["refactored_code"] = (
                f"Refactoring skipped — Code Review findings unavailable: {load_error}. No fixes applied."
            )
            state["workflow_status"] = "needs_human_review"
            logger.error(
                "refactoring: findings unavailable (%s) for run %s", load_error, state.get("run_id")
            )
            return state

        # Only OPEN findings are actionable. Skip Code Review's auto-suppressed false positives
        # (idiomatic test asserts via S101, known-safe auth constants, ...) — mirrors
        # code_review._split(). "Fixing" a suppressed finding would undo the review's own
        # false-positive filtering and can break legitimate code (test files especially).
        actionable = [
            f for f in findings
            if str(f.get("file", "")).strip() and f.get("status") != "Suppressed"
        ]
        if not actionable:
            # Clean review (or only suppressed / project-level notes) — nothing file-scoped to fix.
            state["refactored_code"] = "No actionable (Open) review findings to apply; nothing refactored."
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

        # Don't ask the model to fix files that aren't in the workspace: pre-check existence and
        # report the missing ones as skipped (a file named in the review but absent here is not fatal).
        present: list[str] = []
        skipped: list[str] = []
        for rel in files:
            try:
                executor.read_file(_project_path(project_dir, rel))
                present.append(rel)
            except Exception:  # noqa: BLE001 - missing file is skipped, not fatal
                skipped.append(f"{rel} (not found in workspace)")

        if not present:
            state["generated_code"] = list(state.get("generated_code", []))
            state["refactored_code"] = _summary([], 0, skipped, deferred)
            state["workflow_status"] = "refactored"
            logger.info(
                "refactoring: no present files to fix (skipped %d) for run %s",
                len(skipped), state.get("run_id"),
            )
            return state

        # AGENTIC edit session (like a coding agent): give the model the review findings plus
        # read_file / write_file tools scoped to <project_dir>/, and let it inspect and EDIT the
        # files directly in one tool loop — iterating across files — instead of a per-file one-shot
        # full-file rewrite. It fixes ONLY the findings; the write_file tool records touched paths.
        touched: list[str] = []
        tools = self._editing_tools(executor, project_dir, touched)
        prompt = self._build_prompt(present, by_file)
        notes = self.llm.complete_with_tools(
            prompt=prompt, system=system, tools=tools, max_iters=REFACTOR_MAX_ITERS
        )

        generated = list(state.get("generated_code", []))
        for out_path in touched:
            if out_path not in generated:
                generated.append(out_path)
        applied = sum(len(by_file[rel]) for rel in present) if touched else 0
        state["generated_code"] = generated
        state["refactored_code"] = _summary(sorted(touched), applied, skipped, deferred)
        state["workflow_status"] = "refactored"
        logger.info(
            "refactoring: edited %d file(s), applied ~%d finding(s), skipped %d, deferred %d (run %s) | notes: %s",
            len(touched), applied, len(skipped), len(deferred), state.get("run_id"),
            (notes or "").strip()[:160],
        )
        return state

    # -- agentic editing tools ----------------------------------------------

    def _editing_tools(self, executor: Executor, project_dir: str, touched: list[str]) -> list[Any]:
        """Project-scoped read/write tools the model drives itself (the agentic edit loop).

        Paths are repo-relative; both handlers resolve them under ``<project_dir>/`` (via
        ``_project_path``, which won't double-prefix an already-prefixed path). ``write_file``
        records every path it saves in ``touched`` so the caller knows what changed. A failed read
        returns an error string (never raises) so the model can recover — see llm_gateway._run_tool.
        """
        def _read(path: str) -> str:
            try:
                return executor.read_file(_project_path(project_dir, path))
            except Exception as exc:  # noqa: BLE001 - report to the model, don't crash the loop
                return f"ERROR: could not read {path}: {type(exc).__name__}: {exc}"

        def _write(path: str, content: str) -> str:
            out_path = _project_path(project_dir, path)
            executor.write_file(out_path, content)
            if out_path not in touched:
                touched.append(out_path)
            return f"wrote {path} ({len(content)} chars)"

        return [
            RepairTool(
                name="read_file",
                description="Read a file's current text content. Path is repo-relative.",
                handler=_read,
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            ),
            RepairTool(
                name="write_file",
                description=(
                    "Save the corrected FULL content of a file (overwrites it). Path is repo-relative. "
                    "Use this to apply each fix."
                ),
                handler=_write,
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                },
            ),
        ]

    # -- findings ------------------------------------------------------------

    def _load_findings(self, state: WorkflowState) -> tuple[list[dict[str, Any]], str | None]:
        """Load the review's structured findings from ``review_findings_path`` (the JSON list
        written by ``CodeReviewAgent._finish``).

        Returns ``(findings, error)``. On success ``error`` is None and ``findings`` may be empty
        (a genuinely clean review). On a missing / unreadable / malformed path ``error`` carries a
        human-readable reason so the caller can surface it rather than silently no-op.

        There is deliberately NO Markdown fallback: the persisted ``review_report`` renders findings
        as Markdown tables (sections 4.1/4.2), not a parseable JSON block, so a "parse the report"
        path would be dead code that masks a real upstream failure.
        """
        path = (state.get("review_findings_path") or "").strip()
        if not path:
            return [], "no review_findings_path on state (Code Review did not record findings)"
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return [], f"could not read findings JSON at {path} ({type(exc).__name__})"
        if not isinstance(raw, list):
            return [], f"findings JSON at {path} is not a list"
        return [f for f in raw if isinstance(f, dict)], None

    # -- prompt --------------------------------------------------------------

    @staticmethod
    def _build_prompt(files: list[str], by_file: dict[str, list[dict[str, Any]]]) -> str:
        """One agentic instruction covering every flagged file. The model drives: it reads each
        file with read_file, then applies the fix with write_file — no fixed output format."""
        blocks = []
        for rel in files:
            lines = "\n".join(_finding_line(f) for f in by_file[rel]) or "(no detail)"
            blocks.append(f"File: {rel}\n{lines}")
        joined = "\n\n".join(blocks)
        return (
            "The code review flagged the issues below. Fix them by EDITING the files directly with "
            "the tools: call read_file to see a file's current content, then write_file to save the "
            "corrected FULL file. Work through every file listed. Apply ONLY the fixes the findings "
            "call for — do not rewrite unrelated code, restyle, or change behavior. Paths are "
            "repo-relative; pass them exactly as shown (do not add any prefix).\n\n"
            f"{joined}\n\n"
            "When every fix has been written, reply with a one-line summary of what you changed."
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
    logger.info("================ AGENT: Refactoring ================")
    logger.info("   -> applying the code review's findings to the generated files")
    return _refactoring_agent.execute(state)
