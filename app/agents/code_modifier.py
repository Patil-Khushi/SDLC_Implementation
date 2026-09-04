"""Code Modifier — implement ONE planned work item against an existing codebase.

Brownfield's peer of ``CodeGeneratorAgent``, and deliberately a different agent rather than a flag
on it. The generator never calls ``read_file`` (verified: it only writes) because it is create-only
by contract — every file it emits is new. Pointed at a repository it did not write, that same
behaviour is a blind clobber: it would replace existing files with whatever the model imagined,
having never looked at them.

So brownfield edits go through the shared agentic loop instead (``app/agents/editing.py``), the same
one the Refactoring agent uses: the model reads a file, then writes its complete corrected content.
Two differences from Refactoring:

* **Writes are confined to the work item's target files.** The plan was validated path-by-path
  (existence, forbidden paths, disjointness, caps), and that validation is only meaningful if the
  edit stays inside it. Refactoring needs no such fence — its findings list IS its authorization.
* **A missing target is a CREATE, not a skip.** Refactoring skips a file it cannot read, because a
  finding about a nonexistent file is stale. Here a target that does not exist is one the plan
  explicitly asked for (``action="create"``), so it must be written.

Owns: ``codegen_ok`` (the router's signal), ``changed_files``, and appends to ``generated_code``.
Never commits, never runs a gate — ``change_gate_node`` verifies the edit and a fixed publish node
persists it.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.base import BaseAgent
from app.agents.editing import editing_tools, project_path
from app.graph.state import WorkflowState
from app.integrations.executor import Executor, get_executor
from app.services.change_plan import normalize_target
from app.services.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)

#: Tool turns per target file: one to read it, one to write it, one for the model to reconsider
#: after seeing what is actually there — brownfield code is unfamiliar, so the extra turn matters
#: more than it does when editing code the pipeline generated itself.
MODIFY_ITERS_PER_FILE = 3

#: Floor and ceiling on the scaled budget. The floor covers a single-file change that still needs
#: the model to read a caller or two; the ceiling bounds one item regardless.
MODIFY_MIN_ITERS = 12
MODIFY_MAX_ITERS = 40

#: Cap on how much of an existing file is inlined into the prompt as orientation. The model can
#: always `read_file` for the full text — this is just enough to know what it is looking at.
MAX_PREVIEW_CHARS = 4_000


class CodeModifierAgent(BaseAgent):
    name = "code_modifier"

    def __init__(self, executor: Executor | None = None, llm: LLMGateway | None = None) -> None:
        super().__init__()
        if llm is not None:  # allow test/DI override of the gateway singleton
            self.llm = llm
        self._executor = executor

    def _resolve_executor(self) -> Executor:
        return self._executor if self._executor is not None else get_executor()

    @staticmethod
    def _max_iters_for(file_count: int) -> int:
        scaled = file_count * MODIFY_ITERS_PER_FILE
        return max(MODIFY_MIN_ITERS, min(scaled, MODIFY_MAX_ITERS))

    def execute(self, state: WorkflowState) -> WorkflowState:
        executor = self._resolve_executor()
        project_dir = state.get("project_id") or state.get("run_id") or "project"
        run_id = state.get("run_id") or "-"
        item = state.get("current_work_item")

        if item is None:
            return self._fail(state, "no current work item to implement")
        targets = [normalize_target(p) for p in (item.target_files or [])]
        if not targets:
            return self._fail(state, f"work item '{item.id}' names no target files")

        # A failed gate_result means the router sent us back to retry. Own the counter here rather
        # than in the router (which stays pure), exactly as repair_node owns repair_attempt on the
        # greenfield lane; select_work_item_node resets it to 0 for each new item.
        previous = state.get("gate_result")
        retry_of = previous if previous and not previous.get("passed") else None
        if retry_of is not None:
            state["repair_attempt"] = int(state.get("repair_attempt", 0)) + 1
            logger.info("[code_modifier] run=%s | item=%s | retry %d after a failed gate",
                        run_id, item.id, state["repair_attempt"])

        prompt = self._build_prompt(executor, project_dir, item, targets, state, retry_of)
        touched: list[str] = []
        # allowed_paths is the fence: the plan was validated file-by-file, so an edit outside it
        # would be an unreviewed change wearing an approved plan's authorization.
        tools = editing_tools(
            executor, project_dir, touched, allowed_paths=set(targets), label=self.name,
        )

        logger.info("[code_modifier] run=%s | item=%s | editing %d file(s): %s",
                    run_id, item.id, len(targets), ", ".join(targets))
        try:
            notes = self.llm.complete_with_tools(
                prompt=prompt,
                system=self._load_prompt("code_modification"),
                tools=tools,
                max_iters=self._max_iters_for(len(targets)),
            )
        except Exception as exc:  # noqa: BLE001 - a model failure is a stopped item, not a crash
            logger.exception("[code_modifier] run=%s | item=%s | the edit call failed", run_id, item.id)
            return self._fail(state, f"the edit call for '{item.id}' failed: {type(exc).__name__}: {exc}")

        generated = list(state.get("generated_code") or [])
        changed = list(state.get("changed_files") or [])
        for path in touched:
            if path not in generated:
                generated.append(path)
            if path not in changed:
                changed.append(path)
        state["generated_code"] = generated
        state["changed_files"] = changed
        state["modifier_notes"] = (notes or "").strip()

        # codegen_ok drives route_after_modify. False when the model wrote nothing: the change gate
        # would fail it anyway, but failing here says WHY (the model declined) instead of leaving
        # the gate to report the symptom (nothing changed).
        state["codegen_ok"] = bool(touched)
        if not touched:
            logger.warning("[code_modifier] run=%s | item=%s | the model wrote nothing: %s",
                           run_id, item.id, (notes or "")[:300])
            return _note(state, f"[modify] '{item.id}' changed nothing - {(notes or '').strip()[:400]}")

        rel_touched = [p[len(project_dir) + 1:] if p.startswith(f"{project_dir}/") else p
                       for p in touched]
        logger.info("[code_modifier] run=%s | item=%s | wrote %d file(s): %s",
                    run_id, item.id, len(touched), ", ".join(rel_touched))
        return _note(state, f"[modify] '{item.id}' edited {len(touched)} file(s): "
                            f"{', '.join(rel_touched)}")

    # -- prompt --------------------------------------------------------------

    def _build_prompt(
        self, executor: Executor, project_dir: str, item: Any, targets: list[str],
        state: WorkflowState, retry_of: dict | None = None,
    ) -> str:
        change_request = state.get("change_request") or {}
        parts = [
            # The ReplayGateway test harness keys recorded fixtures off this exact line
            # (conftest.py::_key_from_prompt matches r"Work item:\s*(\S+)"), so it must stay.
            f"Work item: {item.id}",
            "",
            "## What to change",
            "",
            item.change_intent or "(no instruction was recorded for this item)",
            "",
        ]
        criteria = list(item.acceptance_criteria or []) or list(
            change_request.get("acceptance_criteria") or []
        )
        if criteria:
            parts += ["### It is done when", ""] + [f"- {c}" for c in criteria] + [""]

        if description := str(change_request.get("description") or "").strip():
            parts += ["### The overall change request (for context)", "", description, ""]

        parts += ["## Files you may edit", ""]
        for rel in targets:
            try:
                content = executor.read_file(project_path(project_dir, rel))
            except Exception:  # noqa: BLE001 - a target that does not exist is one to CREATE
                parts.append(f"- `{rel}` — **does not exist yet; create it**")
                continue
            preview = content[:MAX_PREVIEW_CHARS]
            truncated = " … (truncated — use read_file for the full text)" if len(content) > MAX_PREVIEW_CHARS else ""
            parts += [f"- `{rel}` ({len(content):,} chars)", "", "```", preview + truncated, "```", ""]

        if retry_of is not None:
            # Tell the model exactly what the gate objected to. Its failure text is written to be
            # actionable ("no change detected in X", "file(s) changed that no work item claimed"),
            # so handing it back verbatim is the whole retry mechanism.
            reasons = [c.get("stderr", "") for c in retry_of.get("checks", []) if not c.get("passed")]
            parts += [
                "", "## Your previous attempt was REJECTED", "",
                *(f"- {r}" for r in reasons if r),
                "", "Fix exactly that and try again.",
            ]

        parts += ["", "Read each file, make the change, and write the complete corrected content."]
        return "\n".join(parts)

    # -- failure -------------------------------------------------------------

    def _fail(self, state: WorkflowState, reason: str) -> WorkflowState:
        logger.warning("[code_modifier] %s", reason)
        state["codegen_ok"] = False
        return _note(state, f"[modify] FAILED - {reason}")


def _note(state: WorkflowState, line: str) -> WorkflowState:
    state["generation_summary"] = (state.get("generation_summary") or "") + line.rstrip("\n") + "\n"
    return state
