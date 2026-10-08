"""The planner that turns an utterance into a plan patch.

Two properties matter more than the happy path.

**It must never name a file for a modify/remove.** Paths come from the requirement ledger, in code,
after the planner replies. That is what confines a conversational turn to files some known work item
actually produced. A reply that smuggles paths in is refused rather than half-honoured.

**It must never raise.** A message typed while work item 23 of 59 is mid-flight cannot be allowed to
kill a multi-hour build, so a garbled reply, a wrong shape and a dead gateway all come back as
refusals — which are successful, reportable outcomes.
"""

from __future__ import annotations

import json
from typing import Any

from app.agents.chat_planner import ChatPlannerAgent
from app.graph.state import WorkflowState, new_state
from app.models import WorkItem
from app.services import requirement_ledger as rl
from app.services.llm_gateway import FakeLLMGateway, LLMGateway


def _plan() -> list[WorkItem]:
    return [
        WorkItem(id="WI-001", feature_title="Login", requirement_ids=["REQ-1"],
                 target_files=["backend/login.py"]),
        WorkItem(id="WI-002", feature_title="Wishlist", requirement_ids=["REQ-7"],
                 target_files=["backend/wishlist.py"]),
    ]


def _state(messages: list[str], *, cursor: int = 1) -> WorkflowState:
    state = new_state(run_id="r", attempt=0, project_id="p", chat_enabled=True)
    state["work_items"] = _plan()
    state["work_item_index"] = cursor
    state["chat_pending"] = [{"seq": i + 1, "text": t} for i, t in enumerate(messages)]
    state["requirement_ledger"] = rl.record([], _plan()[0], ["backend/login.py"])
    return state


_GOOD = json.dumps({
    "ops": [{"op": "modify", "target_work_item_ids": ["WI-001"], "intent": "use OTP"}],
    "notes": "narrowed to the login item",
    "refusal": "",
})


def test_a_well_formed_reply_becomes_a_patch() -> None:
    llm = FakeLLMGateway([_GOOD])
    out = ChatPlannerAgent(llm=llm).execute(_state(["make login use OTP"]))

    patch = out["chat_patch"]
    assert patch["refusal"] == ""
    assert len(patch["ops"]) == 1
    assert patch["ops"][0]["op"] == "modify"
    assert patch["ops"][0]["target_work_item_ids"] == ["WI-001"]
    assert out["chat_planner_notes"] == "narrowed to the login item"
    assert len(llm.calls) == 1, "exactly one model call per turn"


def test_a_fenced_reply_is_tolerated() -> None:
    out = ChatPlannerAgent(llm=FakeLLMGateway([f"```json\n{_GOOD}\n```"])).execute(
        _state(["make login use OTP"])
    )
    assert out["chat_patch"]["ops"][0]["target_work_item_ids"] == ["WI-001"]


def test_prose_around_the_object_is_tolerated() -> None:
    out = ChatPlannerAgent(llm=FakeLLMGateway([f"Here you go:\n{_GOOD}\nHope that helps."])).execute(
        _state(["make login use OTP"])
    )
    assert out["chat_patch"]["ops"][0]["op"] == "modify"


def test_an_explicit_refusal_is_carried_through() -> None:
    reply = json.dumps({"ops": [], "notes": "", "refusal": "too vague to locate"})
    out = ChatPlannerAgent(llm=FakeLLMGateway([reply])).execute(_state(["make it better"]))
    assert out["chat_patch"]["refusal"] == "too vague to locate"
    assert out["chat_planner_notes"] == "too vague to locate"


def test_unparseable_json_degrades_to_a_refusal() -> None:
    out = ChatPlannerAgent(llm=FakeLLMGateway(["I'm afraid I can't do that."])).execute(
        _state(["do something"])
    )
    assert "not valid JSON" in out["chat_patch"]["refusal"]
    assert out["chat_patch"]["ops"] == []


def test_a_wrong_shape_degrades_to_a_refusal() -> None:
    out = ChatPlannerAgent(llm=FakeLLMGateway([json.dumps({"ops": "not-a-list"})])).execute(
        _state(["do something"])
    )
    assert "did not match the expected shape" in out["chat_patch"]["refusal"]


def test_an_unknown_op_value_degrades_to_a_refusal() -> None:
    reply = json.dumps({"ops": [{"op": "rename", "target_work_item_ids": ["WI-001"]}]})
    out = ChatPlannerAgent(llm=FakeLLMGateway([reply])).execute(_state(["rename the thing"]))
    assert out["chat_patch"]["refusal"]


def test_naming_files_on_a_modify_is_refused() -> None:
    # The safety property: paths are resolved from the ledger, never proposed by the model.
    reply = json.dumps({"ops": [{
        "op": "modify", "target_work_item_ids": ["WI-001"], "intent": "x",
        "new_items": [{"id": "X", "target_files": ["../../etc/passwd"]}],
    }]})
    out = ChatPlannerAgent(llm=FakeLLMGateway([reply])).execute(_state(["edit login"]))
    assert "tried to name files" in out["chat_patch"]["refusal"]
    assert out["chat_patch"]["ops"] == []


def test_add_may_name_files_because_they_do_not_exist_yet() -> None:
    reply = json.dumps({"ops": [{
        "op": "add", "intent": "logout endpoint",
        "new_items": [{"id": "WI-100", "target_files": ["backend/logout.py"]}],
    }]})
    out = ChatPlannerAgent(llm=FakeLLMGateway([reply])).execute(_state(["add a logout endpoint"]))
    assert out["chat_patch"]["refusal"] == ""
    assert out["chat_patch"]["ops"][0]["new_items"][0]["id"] == "WI-100"


def test_a_dead_gateway_becomes_a_refusal_not_an_exception() -> None:
    class Dead(LLMGateway):
        def __init__(self) -> None:
            pass

        def complete(self, prompt: str, *, system: str | None = None, **kwargs: Any) -> str:
            raise ConnectionError("gateway down")

    out = ChatPlannerAgent(llm=Dead()).execute(_state(["make login use OTP"]))
    assert "could not be reached" in out["chat_patch"]["refusal"]


def test_no_pending_messages_is_a_clean_no_op() -> None:
    llm = FakeLLMGateway([])
    state = _state([])
    out = ChatPlannerAgent(llm=llm).execute(state)
    assert out["chat_patch"] == {}
    assert out["chat_planner_notes"] == ""
    assert llm.calls == [], "no model call when there is nothing to plan"


# --------------------------------------------------------------- the prompt

def test_the_prompt_marks_generated_and_pending_items() -> None:
    llm = FakeLLMGateway([_GOOD])
    ChatPlannerAgent(llm=llm).execute(_state(["make login use OTP"], cursor=1))

    prompt = llm.calls[0]["prompt"]
    assert "WI-001 [GENERATED]" in prompt, "changing a generated item costs a follow-up edit"
    assert "WI-002 [PENDING]" in prompt, "a pending item is changed in place, which is cheaper"
    assert "make login use OTP" in prompt


def test_the_prompt_does_not_show_the_model_any_file_paths() -> None:
    # The surest way to stop the planner naming files is to never show it one.
    llm = FakeLLMGateway([_GOOD])
    ChatPlannerAgent(llm=llm).execute(_state(["make login use OTP"]))
    plan_section = llm.calls[0]["prompt"].split("WHAT HAS BEEN WRITTEN")[0]
    assert "backend/login.py" not in plan_section
    assert "backend/wishlist.py" not in plan_section


def test_several_messages_are_all_shown_in_one_turn() -> None:
    llm = FakeLLMGateway([_GOOD])
    ChatPlannerAgent(llm=llm).execute(_state(["drop the wishlist", "and add logout"]))
    prompt = llm.calls[0]["prompt"]
    assert "drop the wishlist" in prompt and "and add logout" in prompt


def test_an_overlong_message_is_truncated() -> None:
    llm = FakeLLMGateway([_GOOD])
    ChatPlannerAgent(llm=llm).execute(_state(["x" * 10_000]))
    assert len(llm.calls[0]["prompt"]) < 9_000
