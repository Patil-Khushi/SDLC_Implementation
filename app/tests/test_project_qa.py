"""Answering questions about a run, and refusing to act on change requests.

The chat channel used to do exactly one thing — mutate the plan — so a question came back as the
change planner's refusal. This agent is the other intent on the same channel, and the two properties
worth pinning are:

* **it routes**: a change request is handed back, never attempted here, and
* **it cannot act**: the tools it is given are read-only, so the capability to edit is absent
  rather than guarded.

The third theme is that it never raises. It runs inside an HTTP handler with a user waiting, so a
dead gateway or a garbled reply has to come back as a sentence, not a 500.
"""

from __future__ import annotations

import json
from typing import Any

from app.agents.project_qa import ProjectQAAgent, build_facts_for
from app.integrations.executor import FakeExecutor
from app.models import WorkItem
from app.services.llm_gateway import FakeLLMGateway, LLMGateway
from app.services.run_facts import build_facts, has_run

PROJECT = "demo"


def _reply(kind: str = "question", answer: str = "42 items remain.", notes: str = "") -> str:
    return json.dumps({"kind": kind, "answer": answer, "notes": notes})


# --------------------------------------------------------------- routing

def test_a_question_is_answered():
    llm = FakeLLMGateway([_reply("question", "17 work items remain.")])
    out = ProjectQAAgent(llm=llm).ask("how many items are left?", facts="work items: 59")

    assert out.kind == "question"
    assert out.answer == "17 work items remain."
    assert out.error == ""
    assert len(llm.calls) == 1, "one model call per message"


def test_a_change_request_is_handed_back_not_attempted():
    llm = FakeLLMGateway([_reply("change", "")])
    out = ProjectQAAgent(llm=llm).ask("drop the wishlist feature", facts="work items: 59")
    assert out.kind == "change"


def test_a_mixed_message_routes_as_change_but_keeps_the_explanation():
    # "why is X broken, and fix it" — the user gets the answer now, the fix goes to the planner.
    llm = FakeLLMGateway([_reply("change", "Login fails because the OTP field is missing.")])
    out = ProjectQAAgent(llm=llm).ask("why is login broken, can you fix it?", facts="x")
    assert out.kind == "change"
    assert "OTP" in out.answer, "the question half must still reach the user"


# --------------------------------------------------------------- robustness

def test_a_reply_with_no_json_is_treated_as_the_answer():
    # A model that ignores the format but answers well should not have its answer discarded.
    llm = FakeLLMGateway(["There are 17 work items left, all frontend."])
    out = ProjectQAAgent(llm=llm).ask("how many left?", facts="x")
    assert out.kind == "question"
    assert out.answer == "There are 17 work items left, all frontend."
    assert out.error == ""


def test_a_fenced_reply_is_tolerated():
    llm = FakeLLMGateway([f"```json\n{_reply('question', 'Nine.')}\n```"])
    assert ProjectQAAgent(llm=llm).ask("how many?", facts="x").answer == "Nine."


def test_an_empty_reply_is_an_error_not_an_empty_bubble():
    out = ProjectQAAgent(llm=FakeLLMGateway([""])).ask("anything?", facts="x")
    assert out.error
    assert out.answer == ""


def test_an_empty_question_never_calls_the_model():
    llm = FakeLLMGateway([])
    out = ProjectQAAgent(llm=llm).ask("   ", facts="x")
    assert out.error
    assert llm.calls == []


def test_a_dead_gateway_becomes_an_error_not_an_exception():
    class Dead(LLMGateway):
        def __init__(self) -> None:
            pass

        def complete(self, prompt: str, *, system: str | None = None, **kw: Any) -> str:
            raise ConnectionError("gateway down")

        def complete_with_tools(self, prompt: str, **kw: Any) -> str:
            raise ConnectionError("gateway down")

    out = ProjectQAAgent(llm=Dead()).ask("how many?", facts="x")
    assert "could not be reached" in out.error


def test_an_overlong_question_is_truncated():
    llm = FakeLLMGateway([_reply()])
    ProjectQAAgent(llm=llm).ask("x" * 10_000, facts="")
    assert len(llm.calls[0]["prompt"]) < 9_000


# --------------------------------------------------------------- tools

def test_it_gets_read_only_tools_and_no_way_to_write():
    seen: dict[str, Any] = {}

    class Capturing(LLMGateway):
        def __init__(self) -> None:
            pass

        def complete(self, prompt: str, *, system: str | None = None, **kw: Any) -> str:
            raise AssertionError("with an executor it must use the tool path")

        def complete_with_tools(self, prompt: str, *, system: str | None = None,
                                tools: list | None = None, max_iters: int = 6) -> str:
            seen["tools"] = [t.name for t in (tools or [])]
            return _reply("question", "read it")

    executor = FakeExecutor(files={f"{PROJECT}/backend/login.py": "def login(): ...\n"})
    out = ProjectQAAgent(executor=executor, llm=Capturing()).ask(
        "what does login.py do?", facts="x", project_dir=PROJECT
    )

    assert out.answer == "read it"
    assert sorted(seen["tools"]) == ["list_files", "read_file"]
    assert "write_file" not in seen["tools"], "the agent must have no way to modify the project"


def test_without_an_executor_it_still_answers_from_the_facts_alone():
    llm = FakeLLMGateway([_reply("question", "59 items.")])
    out = ProjectQAAgent(llm=llm).ask("how many?", facts="work items: 59 total")
    assert out.answer == "59 items."


def test_the_facts_are_in_the_prompt():
    llm = FakeLLMGateway([_reply()])
    ProjectQAAgent(llm=llm).ask("status?", facts="status: completed\nwork items: 59 total")
    prompt = llm.calls[0]["prompt"]
    assert "status: completed" in prompt
    assert "work items: 59 total" in prompt
    assert "status?" in prompt


def test_no_run_says_so_rather_than_pretending():
    llm = FakeLLMGateway([_reply()])
    ProjectQAAgent(llm=llm).ask("what was built?", facts="")
    assert "no run has been started" in llm.calls[0]["prompt"]


# --------------------------------------------------------------- the facts digest

def _state() -> dict[str, Any]:
    return {
        "project_id": PROJECT,
        "workflow_status": "completed",
        "work_items": [
            WorkItem(id="WI-001", feature_title="Login", requirement_ids=["REQ-1"]),
            WorkItem(id="WI-002", feature_title="Cart"),
            WorkItem(id="WI-003", feature_title="Wishlist"),
        ],
        "work_item_index": 2,
        "generated_code": [f"{PROJECT}/backend/f{i}.js" for i in range(120)],
        "unit_tests": [f"{PROJECT}/tests/t{i}.js" for i in range(8)],
        "requirement_ledger": [
            {"work_item_id": "WI-001", "requirement_ids": ["REQ-1"], "files": ["backend/login.js"]},
        ],
        "repo_url": "https://github.com/me/demo",
        "security_verdict": "approve",
        "review_report_path": "reports/demo/review.md",
        "generation_summary": "line\n" * 5,
    }


def test_the_digest_carries_exact_counts_and_progress():
    facts = build_facts(_state(), meta={"project": PROJECT, "status": "completed"})
    assert "source files written: 120" in facts
    assert "test files written: 8" in facts
    assert "3 total, 2 selected so far" in facts
    assert "remaining (not yet started): 1" in facts
    assert "https://github.com/me/demo" in facts


def test_the_digest_caps_listings_but_keeps_the_true_count():
    facts = build_facts(_state())
    # 120 paths must not all be rendered, but the count must still be exact.
    assert "source files written: 120" in facts
    assert facts.count(f"{PROJECT}/backend/f") <= 30
    assert "more" in facts


def test_the_digest_distinguishes_not_run_from_nothing_found():
    facts = build_facts({"workflow_status": "running"})
    assert "none written yet" in facts, "an unrun phase must not read as an empty result"


def test_the_digest_reports_traceability_and_reports():
    facts = build_facts(_state())
    assert "WI-001" in facts and "REQ-1" in facts
    assert "reports/demo/review.md" in facts
    assert "verdict: approve" in facts


def test_the_digest_survives_an_empty_state():
    facts = build_facts({})
    assert "(unknown)" in facts
    assert "work items: none" in facts


def test_the_digest_reports_a_brownfield_run_as_such():
    facts = build_facts({"source_mode": "brownfield", "source_repo_url": "https://github.com/a/b"})
    assert "brownfield" in facts
    assert "https://github.com/a/b" in facts


def test_has_run_distinguishes_a_cold_server():
    assert has_run({}) is False
    assert has_run({}, {"project": "demo"}) is True
    assert has_run({"generated_code": ["a.js"]}) is True


def test_build_facts_for_maps_the_servers_last_run_shape():
    facts = build_facts_for({
        "project": "demo", "mode": "real", "status": "completed",
        "started_at": "2026-01-01T00:00:00Z", "state": _state(),
    })
    assert "project: demo" in facts
    assert "mode: real" in facts
    assert "source files written: 120" in facts


def test_build_facts_for_handles_no_run_at_all():
    assert "no run" in build_facts_for(None).lower() or "(unknown)" in build_facts_for(None)


def test_the_digest_handles_a_work_item_with_no_feature_title():
    # The first real run broke here: `getattr(x, n, None) or x.get(n)` falls through to .get()
    # whenever the attribute is present but EMPTY — and a WorkItem has no .get, so an untitled
    # item (entirely normal) raised AttributeError deep inside an HTTP handler.
    facts = build_facts({
        "work_items": [WorkItem(id="WI-009")],  # no feature_title, no action override
        "work_item_index": 0,
    })
    assert "WI-009" in facts
    assert "[create, pending]" in facts


def test_the_digest_handles_work_items_as_plain_dicts():
    # After a checkpoint round-trip the plan comes back as dicts, not models.
    facts = build_facts({
        "work_items": [
            {"id": "WI-001", "feature_title": "Login", "action": "modify"},
            {"id": "WI-002"},
        ],
        "work_item_index": 1,
        "current_work_item": {"id": "WI-002"},
    })
    assert "WI-001 [modify, done] — Login" in facts
    assert "WI-002 [create, pending]" in facts
    assert "current work item: WI-002" in facts


def test_the_digest_handles_a_current_item_as_a_model():
    facts = build_facts({
        "work_items": [WorkItem(id="WI-001")],
        "work_item_index": 1,
        "current_work_item": WorkItem(id="WI-001"),
    })
    assert "current work item: WI-001" in facts
