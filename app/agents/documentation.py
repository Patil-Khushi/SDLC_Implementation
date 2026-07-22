"""Documentation Agent — pure LLM, no sandbox, no file writes.

Runs after Unit Test passes, once the project's final generated source is settled. Reads the
generated file contents and a design-package artifact excerpt (style guide, if present), and asks
the LLM for a single README-style Markdown document.

Deliberately the simplest agent in the pipeline: no tools, no executor writes, no commit. It
writes only `state["documentation"]` - there is nothing to parse back (unlike Debugging, which
returns `{"files": [...]}"` JSON to be written and committed), so this uses the same plain
`self.llm.complete(...)` call the DEVELOPER_GUIDE's own worked example (Code Review) shows for
the "just ask the LLM and keep its answer" case.
"""

from __future__ import annotations

import logging

from app.agents.base import BaseAgent
from app.graph.state import WorkflowState
from app.integrations.executor import Executor, get_executor
from app.services.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)

_MAX_FILES = 25
_MAX_FILE_CHARS = 20_000


class DocumentationAgent(BaseAgent):
    name = "documentation"

    def __init__(self, executor: Executor | None = None, llm: LLMGateway | None = None) -> None:
        super().__init__()
        if llm is not None:
            self.llm = llm
        self._executor = executor

    def _resolve_executor(self) -> Executor:
        return self._executor if self._executor is not None else get_executor()

    def execute(self, state: WorkflowState) -> WorkflowState:
        run_id = state.get("run_id") or "-"
        executor = self._resolve_executor()
        paths = list(state.get("generated_code", []))
        logger.info("[documentation] run=%s | [1/2] reading %d source file(s)...", run_id, len(paths))
        code_context = self._read_sources(executor, paths)
        style_guide = _artifact_text(state.get("design_package") or {}, "SKILL.md", "style-guide/SKILL.md")

        sections = []
        if style_guide.strip():
            sections.append("## Style guide (SKILL.md)\n" + style_guide.strip())
        sections.append("## Source\n" + code_context)
        sections.append(
            "Write a concise README.md for this project: what it does, how to set it up and run "
            "it, its main endpoints/modules, and any notable architecture decisions visible in "
            "the source above. Ground every claim in the actual source shown - do not invent "
            "endpoints, dependencies, or setup steps that aren't evidenced by it."
        )
        prompt = "\n\n".join(sections)

        logger.info("[documentation] run=%s | [2/2] asking the LLM for a README...", run_id)
        text = self.llm.complete(prompt=prompt, system=self._load_prompt("documentation"))
        state["documentation"] = (text or "").strip()
        state["generation_summary"] = (state.get("generation_summary") or "") + "[documentation] generated README\n"
        return state

    @staticmethod
    def _read_sources(executor: Executor, paths: list[str]) -> str:
        seen: list[str] = []
        for p in paths:
            if p not in seen:
                seen.append(p)
        blocks: list[str] = []
        for path in seen[:_MAX_FILES]:
            try:
                content = executor.read_file(path)
            except Exception:  # noqa: BLE001 - a missing file just means less context for the LLM
                continue
            if len(content) > _MAX_FILE_CHARS:
                content = content[:_MAX_FILE_CHARS] + "\n... (truncated)"
            blocks.append(f"### {path}\n```\n{content}\n```")
        if not blocks:
            return "(no source files available)"
        note = f"\n\n(+{len(seen) - len(blocks)} more)" if len(seen) > len(blocks) else ""
        return "\n\n".join(blocks) + note


def _artifact_text(design_package: dict, *names: str) -> str:
    lowered = {k.lower(): v for k, v in design_package.items()}
    for name in names:
        value = design_package.get(name)
        if value is None:
            value = lowered.get(name.lower())
        if value is not None:
            return value if isinstance(value, str) else str(value)
    return ""


# Instantiated once in app/graph/nodes.py (mirrors CodeReviewAgent/UnitTestAgent's convention);
# no module-level singleton or node wrapper defined here.
