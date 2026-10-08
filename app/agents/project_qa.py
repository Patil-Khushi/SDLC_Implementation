"""Answer questions about a run, and route change requests away from itself. One LLM call.

The change-request channel started out able to do exactly one thing: mutate the plan. Anything that
was not an actionable edit came back as the planner's refusal — so "how many work items are left?"
was answered with "too vague to locate", which is correct for a *change* and useless as a reply to
a *question*.

This agent is the other half of that channel. It classifies the message first, then either answers
it or hands it back as a change request for ``chat_planner`` to handle at the next work-item
boundary.

Two things make it deliberately unlike the rest of the pipeline:

**It is not a graph node.** A question needs no plan mutation, no lock, and no work-item boundary —
it is a read. Routing it through the graph would mean waiting minutes for an answer to "what's left",
so it is answered synchronously in the HTTP handler instead. That is also why it takes no
``WorkflowState``: it reads a snapshot, and writes nothing anywhere.

**It cannot modify anything.** It gets ``reading_tools`` — ``read_file`` and ``list_files`` and
nothing else. The write capability is absent rather than guarded, so no amount of prompt pressure
can turn a question into an edit.

Grounding is the whole game. The facts digest (``app/services/run_facts.py``) carries what the run
actually did; the tools let it read the real generated code. A model asked about a project it cannot
see will describe a plausible one instead, and a plausible answer about your codebase is worse than
"I don't know" — so the prompt insists on citing the facts and the files, and on naming what is
missing when the answer genuinely is not there.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.base import BaseAgent
from app.agents.editing import reading_tools
from app.integrations.executor import Executor
from app.services.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)

#: Tool turns for one question. Enough to list a directory and read a few files; not enough for an
#: open-ended exploration of a 300-file project on someone's idle curiosity.
QA_MAX_ITERS = 8

#: Longest question accepted. A chat message is a sentence or two; anything far longer is a paste,
#: and the classification does not improve with more of it.
MAX_QUESTION_CHARS = 4_000


class QAResult(BaseModel):
    """What the agent decided and said."""

    kind: Literal["question", "change"] = "question"
    answer: str = ""
    #: Set when the model could not be reached or its reply was unusable — the caller shows this
    #: rather than an empty bubble.
    error: str = ""
    notes: str = Field(default="", description="the model's own aside, if any")


class ProjectQAAgent(BaseAgent):
    name = "project_qa"

    def __init__(self, executor: Executor | None = None, llm: LLMGateway | None = None) -> None:
        super().__init__()
        if llm is not None:  # allow test/DI override of the gateway singleton
            self.llm = llm
        self._executor = executor

    def execute(self, state: Any) -> Any:
        """Not a graph node — use :meth:`ask`.

        ``BaseAgent`` requires this, but answering a question is a read that happens in an HTTP
        handler, not a step in the pipeline. Raising rather than quietly returning ``state`` keeps
        it from being wired into the graph by mistake, where it would consume a superstep to do
        nothing.
        """
        raise NotImplementedError(
            "ProjectQAAgent answers out of band; call ask(question, facts=...) instead of execute()"
        )

    def ask(self, question: str, *, facts: str, project_dir: str = "") -> QAResult:
        """Classify ``question`` and, if it is one, answer it from ``facts`` plus the project files.

        Never raises. A dead gateway or an unparseable reply becomes a ``QAResult`` carrying an
        ``error``, because this runs inside an HTTP handler that a user is waiting on — a traceback
        there is a 500 where a sentence would do.
        """
        text = (question or "").strip()[:MAX_QUESTION_CHARS]
        if not text:
            return QAResult(error="the question was empty")

        prompt = self._build_prompt(text, facts)
        system = self._load_prompt("project_qa")

        # Tools only when there is a project on disk to read. Without an executor the agent still
        # answers from the facts alone, which covers most questions about progress and outputs.
        tools = None
        if self._executor is not None and project_dir:
            tools = reading_tools(self._executor, project_dir, label=self.name)

        try:
            if tools:
                raw = self.llm.complete_with_tools(
                    prompt=prompt, system=system, tools=tools, max_iters=QA_MAX_ITERS
                )
            else:
                raw = self.llm.complete(prompt=prompt, system=system)
        except Exception as exc:  # noqa: BLE001 - a gateway failure is a message, not a crash
            logger.warning("[project_qa] model call failed: %s: %s", type(exc).__name__, exc)
            return QAResult(error=f"the assistant could not be reached ({type(exc).__name__})")

        return _parse(raw)

    def _build_prompt(self, question: str, facts: str) -> str:
        return "\n".join([
            "## Facts about this run (authoritative — prefer these over any assumption)",
            facts or "(no run has been started, so there are no facts yet)",
            "",
            "## The user's message",
            question,
            "",
            'Reply with ONE JSON object: {"kind": "question"|"change", "answer": "...", '
            '"notes": "..."}. `answer` is what the user reads, in plain prose — no markdown '
            "headings, no JSON inside it. For a change request, set kind to \"change\" and use "
            "`answer` only for any question part worth answering immediately.",
        ])


def _parse(raw: str) -> QAResult:
    """Read a ``QAResult`` out of the reply, tolerating fences, prose and a bare answer.

    The last fallback matters: a model that ignores the JSON instruction and simply answers the
    question well should not have its answer thrown away. A reply with no JSON in it is treated as
    the answer itself, which is the strictly more useful reading of it.
    """
    text = (raw or "").strip()
    if not text:
        return QAResult(error="the assistant returned an empty reply")

    candidate = text
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1] if "\n" in candidate else candidate
        candidate = candidate.rsplit("```", 1)[0]
    for attempt in (candidate, candidate[candidate.find("{"): candidate.rfind("}") + 1]
                    if "{" in candidate and "}" in candidate else ""):
        if not attempt.strip():
            continue
        try:
            data = json.loads(attempt)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and ("answer" in data or "kind" in data):
            kind = data.get("kind")
            return QAResult(
                kind="change" if kind == "change" else "question",
                answer=str(data.get("answer") or "").strip(),
                notes=str(data.get("notes") or "").strip(),
            )

    # No usable JSON — take the whole reply as the answer rather than discarding it.
    return QAResult(kind="question", answer=text)


def build_facts_for(last_run: dict[str, Any] | None) -> str:
    """Facts for whatever run the server is holding — the bridge's ``_last_run`` shape.

    Lives here rather than in the server so the mapping from that dict to a prompt is testable
    without standing a server up.
    """
    from app.services.run_facts import build_facts

    last_run = last_run or {}
    return build_facts(
        last_run.get("state") or {},
        meta={
            "project": last_run.get("project", ""),
            "pack": last_run.get("pack", ""),
            "mode": last_run.get("mode", ""),
            "status": last_run.get("status", ""),
            "started_at": last_run.get("started_at", ""),
            "finished_at": last_run.get("finished_at", ""),
        },
    )
