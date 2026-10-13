"""The node that turns a queued message into a patched plan, between work items.

``chat_intake_node`` sits on the ``feature_publish -> select`` edge and runs on EVERY loop pass, so
the disabled path has to be a genuine no-op — and the enabled path has to be safe against the plan
it is editing being walked at the same time.

The subtlest requirement is the high-water mark. ``chat_seq`` advances even when the planner refuses
or the patch errors, because the mark is checkpointed: a message left unconsumed would be
re-proposed on every later turn AND by every later ``--resume`` of the same thread, forever.
"""

from __future__ import annotations

import json

import pytest

from app.graph import nodes, router
from app.graph.state import WorkflowState, new_state
from app.integrations.executor import FakeExecutor, set_executor
from app.models import WorkItem
from app.services import chat_inbox
from app.services import requirement_ledger as rl
from app.services.llm_gateway import FakeLLMGateway

PROJECT = "proj"
_AT = "2026-09-04T12:00:00Z"


@pytest.fixture(autouse=True)
def _executor():
    set_executor(FakeExecutor())
    yield
    set_executor(None)


@pytest.fixture(autouse=True)
def _inbox_root(tmp_path, monkeypatch):
    # Point the inbox at tmp_path — the node reads the module default, not an injected root.
    monkeypatch.setattr(chat_inbox, "DEFAULT_ROOT", tmp_path)
    return tmp_path


def _plan() -> list[WorkItem]:
    return [
        WorkItem(id="WI-001", feature_title="Login", requirement_ids=["REQ-1"],
                 target_files=["backend/login.py"]),
        WorkItem(id="WI-002", feature_title="Wishlist", requirement_ids=["REQ-7"],
                 target_files=["backend/wishlist.py"]),
        WorkItem(id="WI-003", feature_title="Wishlist", requirement_ids=["REQ-7"],
                 target_files=["frontend/Wishlist.tsx"]),
    ]


def _state(*, chat: bool = True, cursor: int = 1) -> WorkflowState:
    state = new_state(run_id=PROJECT, attempt=0, project_id=PROJECT, chat_enabled=chat)
    state["work_items"] = _plan()
    state["work_item_index"] = cursor
    state["requirement_ledger"] = rl.record([], _plan()[0], ["backend/login.py"])
    return state


def _reply(ops: list[dict], refusal: str = "") -> str:
    return json.dumps({"ops": ops, "notes": "", "refusal": refusal})


def _patch_planner(monkeypatch, reply: str) -> None:
    from app.agents import chat_planner

    monkeypatch.setattr(
        chat_planner.ChatPlannerAgent, "__init__",
        lambda self, llm=None: object.__setattr__(self, "llm", FakeLLMGateway([reply]))
        or setattr(self, "name", "chat_planner"),
    )


# --------------------------------------------------------------- the no-op paths

def test_disabled_chat_is_a_pure_passthrough():
    chat_inbox.append(PROJECT, "drop the wishlist", received_at=_AT)
    before = _state(chat=False)
    out = nodes.chat_intake_node(dict(before))
    assert out["work_items"] == before["work_items"]
    assert out["chat_seq"] == 0 and out["chat_turns"] == 0
    assert out["chat_log"] == []


def test_an_empty_inbox_is_a_passthrough():
    before = _state()
    out = nodes.chat_intake_node(dict(before))
    assert out["work_items"] == before["work_items"]
    assert out["chat_turns"] == 0


def test_the_turn_cap_stops_draining_without_escalating():
    chat_inbox.append(PROJECT, "drop the wishlist", received_at=_AT)
    state = _state()
    state["chat_turns"] = router.CHAT_TURN_CAP
    out = nodes.chat_intake_node(state)
    assert out["chat_turns"] == router.CHAT_TURN_CAP, "not incremented"
    assert out["chat_seq"] == 0, "the message stays queued"
    assert len(out["work_items"]) == 3


# --------------------------------------------------------------- applying a turn

def test_a_remove_drops_pending_items_and_advances_the_mark(monkeypatch):
    seq = chat_inbox.append(PROJECT, "drop the wishlist", received_at=_AT)
    _patch_planner(monkeypatch, _reply([
        {"op": "remove", "target_work_item_ids": ["WI-002", "WI-003"], "intent": "drop wishlist"}
    ]))

    out = nodes.chat_intake_node(_state(cursor=1))

    assert [i.id for i in out["work_items"]] == ["WI-001"]
    assert out["chat_seq"] == seq
    assert out["chat_turns"] == 1
    assert out["chat_pending"] == [], "drained messages are cleared"
    turn = out["chat_log"][-1]
    assert turn["messages"] == ["drop the wishlist"]
    assert turn["diff"] == {"added": [], "removed": ["WI-002", "WI-003"],
                            "count_before": 3, "count_after": 1}


def test_a_modify_of_a_generated_item_appends_an_edit_item(monkeypatch):
    chat_inbox.append(PROJECT, "make login use OTP", received_at=_AT)
    _patch_planner(monkeypatch, _reply([
        {"op": "modify", "target_work_item_ids": ["WI-001"], "intent": "use OTP"}
    ]))
    out = nodes.chat_intake_node(_state(cursor=1))

    follow = out["work_items"][-1]
    assert follow.id == "WI-001-m1"
    assert follow.action == "modify"
    assert follow.target_files == ["backend/login.py"], "resolved from the ledger"


def test_the_cursor_is_never_moved(monkeypatch):
    chat_inbox.append(PROJECT, "drop the wishlist", received_at=_AT)
    _patch_planner(monkeypatch, _reply([
        {"op": "remove", "target_work_item_ids": ["WI-002"]}
    ]))
    out = nodes.chat_intake_node(_state(cursor=1))
    assert out["work_item_index"] == 1, "intake must not touch the loop cursor"
    assert [i.id for i in out["work_items"]] == ["WI-001", "WI-003"], "the patch really applied"


def test_orphans_accumulate_across_turns(monkeypatch):
    chat_inbox.append(PROJECT, "drop login", received_at=_AT)
    _patch_planner(monkeypatch, _reply([
        {"op": "remove", "target_work_item_ids": ["WI-001"], "intent": "drop login"}
    ]))
    state = _state(cursor=1)
    state["orphan_files"] = ["already/dead.py"]
    out = nodes.chat_intake_node(state)
    assert out["orphan_files"] == ["already/dead.py", "backend/login.py"]


# --------------------------------------------------------------- the high-water mark

def test_a_refusal_still_advances_the_mark(monkeypatch):
    # Otherwise the refused message is re-proposed on every later turn, and — because chat_seq is
    # checkpointed — by every later --resume of this thread.
    seq = chat_inbox.append(PROJECT, "make it better", received_at=_AT)
    _patch_planner(monkeypatch, _reply([], refusal="too vague to locate"))

    out = nodes.chat_intake_node(_state())

    assert out["chat_seq"] == seq
    assert out["chat_turns"] == 1
    assert len(out["work_items"]) == 3, "nothing changed"
    assert out["chat_log"][-1]["report"]["refusal"] == "too vague to locate"


def test_an_unparseable_planner_reply_still_advances_the_mark(monkeypatch):
    seq = chat_inbox.append(PROJECT, "do something", received_at=_AT)
    _patch_planner(monkeypatch, "not json")
    out = nodes.chat_intake_node(_state())
    assert out["chat_seq"] == seq
    assert out["chat_log"][-1]["report"]["refusal"]


def test_several_queued_messages_are_one_turn(monkeypatch):
    chat_inbox.append(PROJECT, "first", received_at=_AT)
    last = chat_inbox.append(PROJECT, "second", received_at=_AT)
    _patch_planner(monkeypatch, _reply([]))

    out = nodes.chat_intake_node(_state())

    assert out["chat_seq"] == last
    assert out["chat_turns"] == 1, "a turn is a drain, not a message"
    assert out["chat_log"][-1]["messages"] == ["first", "second"]


def test_a_second_pass_does_not_reprocess_consumed_messages(monkeypatch):
    chat_inbox.append(PROJECT, "drop the wishlist", received_at=_AT)
    _patch_planner(monkeypatch, _reply([{"op": "remove", "target_work_item_ids": ["WI-002"]}]))

    state = nodes.chat_intake_node(_state(cursor=1))
    assert len(state["work_items"]) == 2

    # No new message: the next pass must be a no-op even though the inbox still holds the line.
    again = nodes.chat_intake_node(state)
    assert len(again["work_items"]) == 2
    assert again["chat_turns"] == 1


def test_an_unreadable_inbox_does_not_stop_the_run(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(chat_inbox, "read_new", boom)
    out = nodes.chat_intake_node(_state())
    assert len(out["work_items"]) == 3, "the build continues"
    assert out["chat_turns"] == 0


# --------------------------------------------------------------- escalate

def test_escalate_reports_chat_requests_it_could_never_apply(caplog):
    # chat_intake only runs on the feature_publish -> select edge, which the escalate path never
    # reaches. A request the run died before planning must land in the record, not vanish.
    chat_inbox.append(PROJECT, "make login use OTP", received_at=_AT)
    chat_inbox.append(PROJECT, "drop the wishlist", received_at=_AT)
    state = _state()

    with caplog.at_level("WARNING"):
        out = nodes.escalate_node(state)

    assert out["workflow_status"] == "needs_human_review"
    assert "make login use OTP" in out["generation_summary"]
    assert "drop the wishlist" in out["generation_summary"]
    assert "NOT APPLIED" in out["generation_summary"]
    assert "never applied" in caplog.text


def test_escalate_says_nothing_about_chat_when_the_queue_is_drained():
    chat_inbox.append(PROJECT, "already handled", received_at=_AT)
    state = _state()
    state["chat_seq"] = 1                      # consumed
    out = nodes.escalate_node(state)
    assert "[chat]" not in (out.get("generation_summary") or "")


def test_escalate_is_silent_about_chat_when_chat_is_disabled():
    chat_inbox.append(PROJECT, "queued but chat is off", received_at=_AT)
    out = nodes.escalate_node(_state(chat=False))
    assert "[chat]" not in (out.get("generation_summary") or "")
    assert out["workflow_status"] == "needs_human_review"


def test_escalate_survives_an_unreadable_inbox(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(chat_inbox, "read_new", boom)
    out = nodes.escalate_node(_state())
    assert out["workflow_status"] == "needs_human_review", "reporting must never break the node"
