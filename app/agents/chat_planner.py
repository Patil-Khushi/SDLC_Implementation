"""Chat Planner — turn an utterance into a structured patch to the plan. One LLM call, no writes.

This agent exists because of standing rule 4: nodes never call the LLM directly. ``chat_intake_node``
owns the mechanics of a conversational turn (drain the inbox, apply the patch, advance the high-water
mark) but the one step that needs a model — reading "drop the wishlist and make login use OTP" and
deciding which work items that is about — lives here.

Its authority is deliberately narrow. It proposes **ops over work-item ids** and prose intent. It
never names a file for a ``modify`` or ``remove``: paths are resolved afterwards, in
``app/services/plan_patch.py``, from the requirement ledger's record of what each item actually
wrote. That split is the safety property. A planner that could name paths could reach any file in
the project from a single sentence, and the resulting plan would not be reviewable; a planner that
can only name work items can only ever touch files some known work item produced.

The other deliberate behaviour is refusal. A request that is too vague to locate, a rename (no call
graph, so no way to find every caller), or a cross-cutting sweep all come back as
``refusal`` — and a refusal is a **successful** outcome, recorded and shown to the user, not an
error. The alternative is a plausible-looking patch aimed at the wrong items, which edits code
nobody asked about and which may already be committed and pushed.

Nothing here raises for a bad model reply either. A turn arriving mid-run must not be able to kill a
multi-hour build, so an unparseable reply — or one that names files instead of ids — degrades to a
refusal.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agents.base import BaseAgent
from app.graph.state import WorkflowState
from app.models import WorkItem
from app.services import requirement_ledger
from app.services.llm_gateway import LLMGateway
from app.services.plan_patch import PlanPatch

logger = logging.getLogger(__name__)

#: Cap on how much of one message is fed to the model. A chat message is a sentence or two; anything
#: vastly longer is a paste, and the plan-patch decision does not improve with more of it.
MAX_MESSAGE_CHARS = 4_000

#: Cap on how many plan items are rendered into the prompt. A 59-item plan is fine; this stops a
#: pathological plan from crowding out the request itself.
MAX_PLAN_ITEMS = 300


class ChatPlannerAgent(BaseAgent):
    name = "chat_planner"

    def __init__(self, llm: LLMGateway | None = None) -> None:
        super().__init__()
        if llm is not None:  # allow test/DI override of the gateway singleton
            self.llm = llm

    def execute(self, state: WorkflowState) -> WorkflowState:
        pending = state.get("chat_pending") or []
        if not pending:
            # Nothing drained this turn. A clean no-op, like every other agent handed no work.
            state["chat_patch"] = {}
            state["chat_planner_notes"] = ""
            return state

        items: list[WorkItem] = state.get("work_items") or []
        cursor = int(state.get("work_item_index", 0))
        ledger = state.get("requirement_ledger") or []

        prompt = self._build_prompt(pending, items, cursor, ledger)
        system = self._load_prompt("chat_patch")

        try:
            raw = self.llm.complete(prompt=prompt, system=system)
        except Exception as exc:  # noqa: BLE001 - a gateway failure must not kill the run
            return self._refuse(
                state, f"the planner could not be reached ({type(exc).__name__}: {exc})"
            )

        patch, reason = _parse_patch(raw)
        if patch is None:
            return self._refuse(state, reason)

        state["chat_patch"] = patch.model_dump()
        state["chat_planner_notes"] = patch.notes or patch.refusal or ""
        logger.info(
            "[chat_planner] %d message(s) -> %d op(s)%s",
            len(pending), len(patch.ops), f" (refused: {patch.refusal})" if patch.refusal else "",
        )
        return state

    def _refuse(self, state: WorkflowState, reason: str) -> WorkflowState:
        """Record a refusal as a successful, reportable outcome — never an exception."""
        logger.info("[chat_planner] refusing this turn: %s", reason)
        state["chat_patch"] = PlanPatch(refusal=reason).model_dump()
        state["chat_planner_notes"] = reason
        return state

    # -- prompt ---------------------------------------------------------------

    def _build_prompt(
        self,
        pending: list[dict[str, Any]],
        items: list[WorkItem],
        cursor: int,
        ledger: list[dict[str, Any]],
    ) -> str:
        return "\n".join([
            "THE USER SAID",
            *[f"- {(m.get('text') or '')[:MAX_MESSAGE_CHARS]}" for m in pending],
            "",
            "THE CURRENT PLAN",
            "Each line is one work item. GENERATED items already have code on disk (and may be "
            "committed and pushed); PENDING items have not been started, so changing one is free.",
            _render_plan(items, cursor, ledger),
            "",
            "WHAT HAS BEEN WRITTEN SO FAR (per work item)",
            requirement_ledger.summarize(ledger),
            "",
            "Reply with the single JSON object described in your instructions.",
        ])


def _render_plan(items: list[WorkItem], cursor: int, ledger: list[dict[str, Any]]) -> str:
    """One line per work item: id, state, feature, requirements.

    Deliberately omits ``target_files``. The planner must not name files for a modify/remove, and
    the surest way to keep it from doing so is not to show it any.
    """
    if not items:
        return "(the plan is empty)"
    lines = []
    for idx, item in enumerate(items[:MAX_PLAN_ITEMS]):
        generated = requirement_ledger.is_generated(ledger, item.id) or idx < cursor
        reqs = ", ".join(item.requirement_ids or []) or "-"
        lines.append(
            f"{item.id} [{'GENERATED' if generated else 'PENDING'}] "
            f"feature={item.feature_title or item.feature_id or '-'} reqs={reqs} "
            f"action={item.action}"
        )
    if len(items) > MAX_PLAN_ITEMS:
        lines.append(f"... and {len(items) - MAX_PLAN_ITEMS} more items not listed")
    return "\n".join(lines)


def _parse_patch(raw: str) -> tuple[PlanPatch | None, str]:
    """Parse a ``PlanPatch`` out of the model's reply. Returns ``(patch, refusal_reason)``.

    Every failure mode becomes a refusal reason rather than an exception: no JSON object, a shape
    pydantic rejects, or — the one that matters most — a modify/remove op that names files instead
    of work items, which would bypass ledger-based path resolution.
    """
    data = _extract_json(raw)
    if data is None:
        return None, "the planner's reply was not valid JSON, so no change was made"

    try:
        patch = PlanPatch(**data)
    except Exception as exc:  # noqa: BLE001 - pydantic/type errors both degrade to a refusal
        return None, f"the planner's reply did not match the expected shape ({exc})"

    for op in patch.ops:
        if op.op in ("modify", "remove") and op.new_items:
            return None, (
                f"the planner tried to name files on a '{op.op}' op; paths are resolved from the "
                "requirement ledger, not proposed by the model, so the turn was refused"
            )
    return patch, ""


def _extract_json(raw: str) -> dict[str, Any] | None:
    """Parse a JSON object out of a reply, tolerating markdown fences and surrounding prose.

    Mirrors ``change_planner._extract_json``'s approach rather than importing it: that one is part
    of the brownfield lane, and the tolerance worth sharing (strip fences, then first-``{`` to
    last-``}``) is three lines.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    candidates = [text]
    if "{" in text and "}" in text:
        candidates.append(text[text.find("{"): text.rfind("}") + 1])
    for candidate in candidates:
        if not candidate.strip():
            continue
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None
