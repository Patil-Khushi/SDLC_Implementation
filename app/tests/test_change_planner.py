"""The Change Planner: prose change request + unfamiliar repo -> a validated plan.

Brownfield's peer of ``plan_builder.build_plan``, but the input is a paragraph and somebody else's
code, so the decomposition is a judgement call and the output is a PROPOSAL. The tests here are
mostly about what happens when that proposal is wrong — a plan naming plausible files the planner
never opened looks exactly like a good one, and everything downstream trusts it.

The stub gateway drives the real tool loop (``read_file`` / ``list_files``) the way the model does,
so tool wiring is exercised without an API key.
"""

from __future__ import annotations

import json
from typing import Any

from app.agents.change_planner import ChangePlannerAgent
from app.graph.nodes import change_plan_node
from app.graph.router import route_after_acquire, route_after_change_plan
from app.integrations.executor import FakeExecutor, set_executor
from app.services.repo_inventory import inventory

_REPO_FILES = {
    "p1/src/auth/token.py": "def verify(t):\n    return True\n",
    "p1/src/auth/login.py": "from .token import verify\n\ndef login(t):\n    return verify(t)\n",
    "p1/src/api/routes.py": "from src.auth.login import login\n",
    "p1/tests/test_login.py": "def test_login(): pass\n",
    "p1/README.md": "# app\n",
}

_CHANGE_REQUEST = {
    "id": "CR-1", "title": "Reject expired tokens", "kind": "bug",
    "description": "verify() accepts expired tokens; it must reject them.",
    "acceptance_criteria": ["An expired token is rejected"],
}

_GOOD_PLAN = {
    "work_items": [{
        "id": "auth-token-expiry",
        "action": "modify",
        "change_intent": "Make verify() check the exp claim and return False when it has passed.",
        "target_files": ["src/auth/token.py"],
        "acceptance_criteria": ["An expired token is rejected"],
    }],
    "notes": "Read token.py and login.py; the expiry check belongs in verify().",
}


class _StubLLM:
    """Replies with a canned plan, optionally driving the read tools first (as the model would)."""

    def __init__(self, reply: Any, *, read: list[str] | None = None) -> None:
        self._reply = reply if isinstance(reply, str) else json.dumps(reply)
        self._read = read or []
        self.prompts: list[str] = []
        self.tool_names: list[str] = []
        self.reads: list[str] = []

    def complete_with_tools(self, prompt: str, *, system: str | None = None,
                            tools: list | None = None, max_iters: int = 6) -> str:
        self.prompts.append(prompt)
        by_name = {t.name: t for t in (tools or [])}
        self.tool_names = sorted(by_name)
        for path in self._read:
            self.reads.append(str(by_name["read_file"].handler(path=path)))
        return self._reply


def _executor() -> FakeExecutor:
    return FakeExecutor(files=dict(_REPO_FILES))


def _state(executor: FakeExecutor, **overrides: Any) -> dict:
    state = {
        "project_id": "p1", "run_id": "r1",
        "source_mode": "brownfield",
        "source_repo_url": "https://github.com/acme/widgets",
        "change_request": dict(_CHANGE_REQUEST),
        "repo_inventory": inventory(executor, "p1").as_dict(),
        "generation_summary": "",
    }
    state.update(overrides)
    return state


def _run_node(executor: FakeExecutor, llm: _StubLLM, **overrides: Any) -> dict:
    """Drive the NODE (planner + validator + blast radius), which is what the graph runs."""
    import app.graph.nodes as nodes_module

    original = nodes_module._change_planner
    nodes_module._change_planner = ChangePlannerAgent(executor=executor, llm=llm)
    set_executor(executor)
    try:
        return change_plan_node(_state(executor, **overrides))
    finally:
        nodes_module._change_planner = original
        set_executor(None)


# --- the happy path -----------------------------------------------------------------------------


def test_a_valid_plan_is_accepted_and_becomes_work_items() -> None:
    out = _run_node(_executor(), _StubLLM(_GOOD_PLAN))

    assert out["workflow_status"] == "change_planned"
    assert out["change_plan_errors"] == []
    assert [i.id for i in out["work_items"]] == ["auth-token-expiry"]
    item = out["work_items"][0]
    assert item.action == "modify"
    assert item.target_files == ["src/auth/token.py"]
    assert item.acceptance_criteria == ["An expired token is rejected"]
    assert "expiry check belongs in verify" in out["change_plan_notes"]


def test_the_planner_gets_read_only_tools() -> None:
    # It decides; a later step edits. A planner that could write would blur the boundary that lets
    # the plan be validated before anything is touched.
    llm = _StubLLM(_GOOD_PLAN)
    _run_node(_executor(), llm)
    assert llm.tool_names == ["list_files", "read_file"]
    assert "write_file" not in llm.tool_names


def test_the_tools_actually_reach_the_repo() -> None:
    llm = _StubLLM(_GOOD_PLAN, read=["src/auth/token.py", "nope.py"])
    _run_node(_executor(), llm)
    assert "def verify" in llm.reads[0]                  # a real file came back
    assert llm.reads[1].startswith("ERROR: could not read")   # a bad path is reported, not raised


def test_the_prompt_carries_the_request_and_the_module_listing() -> None:
    llm = _StubLLM(_GOOD_PLAN)
    _run_node(_executor(), llm)
    prompt = llm.prompts[0]
    assert "Reject expired tokens" in prompt
    assert "An expired token is rejected" in prompt         # acceptance criteria survive
    assert "src/auth" in prompt and "token.py" in prompt    # the module listing is there


def test_the_blast_radius_is_computed_and_reported() -> None:
    # The closest thing to impact analysis: what else imports the file being changed.
    out = _run_node(_executor(), _StubLLM(_GOOD_PLAN))
    assert out["change_impacts"]["src/auth/token.py"] == ["src/auth/login.py"]
    assert "src/auth/login.py" in out["change_report"]


# --- the proposal being wrong -------------------------------------------------------------------


def test_a_plan_naming_a_file_that_does_not_exist_is_rejected() -> None:
    # THE failure this validation exists for: the planner hallucinated a path, and without the
    # check the editing agent would create it instead of changing anything.
    plan = json.loads(json.dumps(_GOOD_PLAN))
    plan["work_items"][0]["target_files"] = ["src/auth/imaginary.py"]
    out = _run_node(_executor(), _StubLLM(plan))

    assert out["work_items"] == []          # nothing may act on a rejected plan
    assert out["workflow_status"] == "needs_human_review"
    assert any("not in the repository" in e for e in out["change_plan_errors"])
    assert "Plan REJECTED" in out["change_report"]


def test_a_forbidden_target_is_rejected() -> None:
    plan = json.loads(json.dumps(_GOOD_PLAN))
    plan["work_items"][0]["target_files"] = [".github/workflows/ci.yml"]
    out = _run_node(_executor(), _StubLLM(plan))
    assert out["work_items"] == []
    assert any("CI workflow" in e for e in out["change_plan_errors"])


def test_an_unparseable_reply_stops_the_run_with_a_reason() -> None:
    out = _run_node(_executor(), _StubLLM("I think you should change the auth module."))
    assert out["work_items"] == []
    assert out["workflow_status"] == "needs_human_review"
    assert "not valid JSON" in out["generation_summary"]


def test_a_reply_wrapped_in_markdown_fences_still_parses() -> None:
    fenced = "```json\n" + json.dumps(_GOOD_PLAN) + "\n```"
    out = _run_node(_executor(), _StubLLM(fenced))
    assert [i.id for i in out["work_items"]] == ["auth-token-expiry"]


def test_an_explicit_refusal_is_reported_with_its_reason() -> None:
    # An empty plan is the planner's documented way of refusing (out of scope / too vague / needs
    # caller analysis it cannot do). That is a legitimate outcome, so the REASON is what matters.
    refusal = {"work_items": [], "notes": "This needs a rename; callers cannot be found here."}
    out = _run_node(_executor(), _StubLLM(refusal))

    assert out["work_items"] == []
    assert out["workflow_status"] == "needs_human_review"
    assert "needs a rename" in out["generation_summary"]


def test_one_malformed_item_does_not_discard_the_whole_plan() -> None:
    plan = json.loads(json.dumps(_GOOD_PLAN))
    plan["work_items"].append({"id": "", "action": "sideways", "target_files": ["src/api/routes.py"]})
    out = _run_node(_executor(), _StubLLM(plan))

    # The good item survived; the bad one is REPORTED, not silently dropped — a quietly shorter
    # plan is indistinguishable from a smaller change request.
    assert "DISCARDED malformed work item" in out["generation_summary"]


def test_a_gateway_failure_stops_the_run_rather_than_crashing() -> None:
    class _Exploding(_StubLLM):
        def complete_with_tools(self, *a: Any, **kw: Any) -> str:
            raise RuntimeError("gateway down")

    out = _run_node(_executor(), _Exploding(_GOOD_PLAN))
    assert out["work_items"] == []
    assert "the planning call failed" in out["generation_summary"]


def test_planning_writes_no_files() -> None:
    ex = _executor()
    _run_node(ex, _StubLLM(_GOOD_PLAN, read=["src/auth/token.py"]))
    assert ex.writes == []
    for path, content in _REPO_FILES.items():
        assert ex.files[path] == content


# --- routing ------------------------------------------------------------------------------------


def test_routing_into_and_out_of_planning() -> None:
    # acquire -> plan only when acquisition actually produced an inventory.
    assert route_after_acquire({"repo_inventory": {"files": ["a.py"]}}) == "change_plan"
    assert route_after_acquire({}) == "escalate"
    # An accepted plan goes on to be implemented; every failure shape (refusal, unparseable reply,
    # rejected plan) leaves work_items empty, so this one check covers all of them.
    assert route_after_change_plan({"work_items": [object()]}) == "select"
    assert route_after_change_plan({"work_items": []}) == "escalate"
