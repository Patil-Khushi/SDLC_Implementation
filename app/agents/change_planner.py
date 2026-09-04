"""Change Planner — decide which files a change request must touch, and why.

Brownfield's answer to ``plan_builder.build_plan``. That one decomposes a design package into work
items by reading structured artifacts; there is no such artifact here — the inputs are a paragraph
of prose and somebody else's repository — so the decomposition is a judgement call and belongs to a
model, not to a deterministic service. (This is why it lives in ``agents/`` and not in
``services/``: the ``services/`` family's whole contract is "no LLM, same input same output".)

The model **browses rather than being fed the repo**. It gets a module listing and read-only
``list_files``/``read_file`` tools, and pulls the files it wants. Shipping the tree instead would
hit the same context ceiling ``code_review._build_llm_context`` works around, and would still leave
the model guessing about anything truncated. Reading on demand also makes the plan honest: a file
it names is usually a file it actually opened.

Everything it returns is a PROPOSAL. ``services/change_plan.validate_plan`` checks it against the
real repository before anything acts on it, because the failure mode here is quiet — a plan naming
plausible files the planner never read looks exactly like a good one, and every step downstream
trusts it.

Owns: ``work_items``, ``change_plan_notes``, and its ``workflow_status`` stamp. Writes no files.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agents.base import BaseAgent
from app.graph.state import WorkflowState
from app.integrations.executor import Executor, RepairTool, get_executor
from app.models import WorkItem
from app.services.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)

#: Tool-loop budget. The planner reads to understand, it does not edit, so it needs fewer turns
#: than the editing loop — but enough to open a handful of files across a few modules.
PLANNER_MAX_ITERS = 14

#: Modules listed in the prompt before the model starts browsing. Enough to orient in a mid-sized
#: repo; beyond this the listing is overflow-reported and the model uses ``list_files`` instead.
MAX_MODULES_IN_PROMPT = 60

#: Cap on a single ``read_file`` result handed back to the model. A planner does not need a 200KB
#: file in full to decide whether it is the right one, and an unbounded read can end the loop by
#: exhausting the turn budget on one file.
MAX_READ_CHARS = 20_000


class ChangePlannerAgent(BaseAgent):
    name = "change_planner"

    def __init__(self, executor: Executor | None = None, llm: LLMGateway | None = None) -> None:
        super().__init__()
        if llm is not None:  # allow test/DI override of the gateway singleton
            self.llm = llm
        self._executor = executor

    def _resolve_executor(self) -> Executor:
        return self._executor if self._executor is not None else get_executor()

    def execute(self, state: WorkflowState) -> WorkflowState:
        executor = self._resolve_executor()
        project_dir = state.get("project_id") or state.get("run_id") or "project"
        run_id = state.get("run_id") or "-"
        change_request = state.get("change_request") or {}
        inv = state.get("repo_inventory") or {}

        if not change_request:
            return self._fail(state, "no change_request in state - nothing to plan")
        if not inv.get("source_files"):
            return self._fail(state, "the repository inventory has no source files to plan against")

        prompt = self._build_prompt(change_request, inv)
        tools = self._reading_tools(executor, project_dir)
        logger.info(
            "[change_planner] run=%s | planning against %d source file(s) in %d module(s)",
            run_id, len(inv.get("source_files", [])), len(inv.get("by_dir", {})),
        )

        try:
            raw = self.llm.complete_with_tools(
                prompt=prompt,
                system=self._load_prompt("change_planner"),
                tools=tools,
                max_iters=PLANNER_MAX_ITERS,
            )
        except Exception as exc:  # noqa: BLE001 - a planner failure is a stopped run, not a crash
            logger.exception("[change_planner] run=%s | the planning call failed", run_id)
            return self._fail(state, f"the planning call failed: {type(exc).__name__}: {exc}")

        parsed = _extract_json(raw)
        if parsed is None:
            logger.warning("[change_planner] run=%s | unparseable reply: %s", run_id, raw[:300])
            return self._fail(state, "the planner's reply was not valid JSON")

        notes = str(parsed.get("notes") or "").strip()
        state["change_plan_notes"] = notes

        items, bad = _coerce_items(parsed.get("work_items") or [])
        state["work_items"] = items

        if bad:
            # Reported, never silently dropped: an item this service could not read is a hole in
            # the plan, and a hole that nobody is told about looks like a smaller change request.
            logger.warning("[change_planner] run=%s | %d unusable item(s): %s", run_id, len(bad), bad)
            state["generation_summary"] = (state.get("generation_summary") or "") + "".join(
                f"[plan] DISCARDED malformed work item: {reason}\n" for reason in bad
            )

        if not items:
            # An empty plan is the planner's documented way of REFUSING (out of scope, too vague,
            # needs caller analysis it cannot do). That is a legitimate outcome with a stated
            # reason, not a malfunction — so the reason is what gets surfaced.
            return self._fail(state, notes or "the planner produced no work items and gave no reason")

        logger.info(
            "[change_planner] run=%s | planned %d item(s) over %d file(s): %s",
            run_id, len(items), sum(len(i.target_files) for i in items),
            ", ".join(i.id for i in items),
        )
        state["workflow_status"] = "change_planned"
        return state

    # -- prompt --------------------------------------------------------------

    def _build_prompt(self, change_request: dict[str, Any], inv: dict[str, Any]) -> str:
        criteria = change_request.get("acceptance_criteria") or []
        constraints = change_request.get("constraints") or []
        parts = [
            "## Change request",
            f"**{change_request.get('title') or change_request.get('id') or 'untitled'}** "
            f"({change_request.get('kind') or 'modification'})",
            "",
            str(change_request.get("description") or "").strip() or "_No description supplied._",
        ]
        if criteria:
            parts += ["", "### Acceptance criteria"] + [f"- {c}" for c in criteria]
        if constraints:
            parts += ["", "### Constraints"] + [f"- {c}" for c in constraints]

        parts += ["", "## Repository", "",
                  f"Primary language: {inv.get('primary_language') or 'unknown'}. "
                  f"{len(inv.get('source_files', []))} source file(s), "
                  f"{len(inv.get('test_files', []))} test file(s)."]
        managers = sorted(set((inv.get("manifests") or {}).values()))
        if managers:
            parts.append(f"Package manager(s): {', '.join(managers)}.")

        by_dir: dict[str, list[str]] = inv.get("by_dir") or {}
        parts += ["", "### Source files by module", ""]
        for directory, files in sorted(by_dir.items())[:MAX_MODULES_IN_PROMPT]:
            parts.append(f"- `{directory or '.'}/` - " + ", ".join(f"`{f.rsplit('/', 1)[-1]}`" for f in files))
        if len(by_dir) > MAX_MODULES_IN_PROMPT:
            parts.append(f"- _(+{len(by_dir) - MAX_MODULES_IN_PROMPT} more modules - use `list_files`)_")

        tests = inv.get("test_files") or []
        if tests:
            parts += ["", "### Test files", ""] + [f"- `{t}`" for t in tests[:30]]
            if len(tests) > 30:
                parts.append(f"- _(+{len(tests) - 30} more)_")

        parts += ["", "Read the files you need, then reply with the JSON plan."]
        return "\n".join(parts)

    # -- tools ---------------------------------------------------------------

    def _reading_tools(self, executor: Executor, project_dir: str) -> list[Any]:
        """READ-ONLY tools. Deliberately the editing loop's toolset minus ``write_file``: the
        planner decides, the implementer edits, and a planner that could write would blur the
        boundary that lets the plan be validated before anything is touched."""

        def _read(path: str) -> str:
            try:
                content = executor.read_file(f"{project_dir}/{path.lstrip('/')}")
            except Exception as exc:  # noqa: BLE001 - report to the model, don't crash the loop
                return f"ERROR: could not read {path}: {type(exc).__name__}: {exc}"
            if len(content) > MAX_READ_CHARS:
                return content[:MAX_READ_CHARS] + f"\n... (truncated at {MAX_READ_CHARS} chars)"
            return content

        def _list(prefix: str = "") -> str:
            try:
                paths = executor.list_files(project_dir, prefix or "")
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: could not list files: {type(exc).__name__}: {exc}"
            if not paths:
                return f"(no files under '{prefix}')"
            shown = paths[:400]
            out = "\n".join(shown)
            return out + (f"\n... (+{len(paths) - len(shown)} more)" if len(paths) > len(shown) else "")

        return [
            RepairTool(
                name="read_file",
                description="Read a file's current content. Path is repo-relative.",
                handler=_read,
                input_schema={"type": "object", "properties": {"path": {"type": "string"}},
                              "required": ["path"]},
            ),
            RepairTool(
                name="list_files",
                description="List repo-relative file paths, optionally filtered by a path prefix.",
                handler=_list,
                input_schema={"type": "object",
                              "properties": {"prefix": {"type": "string"}}, "required": []},
            ),
        ]

    # -- failure -------------------------------------------------------------

    def _fail(self, state: WorkflowState, reason: str) -> WorkflowState:
        """Stop with a stated reason. ``work_items`` is left empty, which is what the router reads
        to send the run to escalate — the reason is for the human who picks it up."""
        logger.warning("[change_planner] %s", reason)
        state["work_items"] = []
        state["workflow_status"] = "needs_human_review"
        state["generation_summary"] = (state.get("generation_summary") or "") + f"[plan] FAILED - {reason}\n"
        return state


def _extract_json(raw: str) -> dict[str, Any] | None:
    """Parse the model's JSON reply, tolerating markdown fences and surrounding prose.

    Mirrors ``code_generator._extract_json``'s approach rather than importing it, because that one
    is shaped around the ``{"files": [...]}`` contract; the tolerance (fences, then first-``{`` to
    last-``}``) is the part worth sharing and it is three lines.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1] if "{" in text and "}" in text else ""):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _coerce_items(raw_items: Any) -> tuple[list[WorkItem], list[str]]:
    """Turn the model's item dicts into validated ``WorkItem``s.

    Each item is coerced INDIVIDUALLY so one malformed entry costs that entry, not the whole plan —
    and every rejection is returned with its reason rather than swallowed, because a silently
    shorter plan is indistinguishable from a smaller change request.
    """
    items: list[WorkItem] = []
    problems: list[str] = []
    if not isinstance(raw_items, list):
        return [], [f"work_items must be a list, got {type(raw_items).__name__}"]

    for index, entry in enumerate(raw_items):
        if not isinstance(entry, dict):
            problems.append(f"item {index} is {type(entry).__name__}, not an object")
            continue
        data = {
            "id": str(entry.get("id") or "").strip() or f"change-{index + 1}",
            "action": str(entry.get("action") or "modify").strip().lower(),
            "change_intent": str(entry.get("change_intent") or "").strip(),
            "target_files": [str(p).strip() for p in (entry.get("target_files") or []) if str(p).strip()],
            "acceptance_criteria": [str(c) for c in (entry.get("acceptance_criteria") or [])],
        }
        try:
            items.append(WorkItem(**data))
        except Exception as exc:  # noqa: BLE001 - pydantic ValidationError and anything it wraps
            problems.append(f"item {index} ('{data['id']}') is invalid: {exc}")
    return items, problems
