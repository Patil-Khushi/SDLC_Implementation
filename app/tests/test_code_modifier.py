"""Editing an existing codebase, and the fences around it.

The Code Modifier is brownfield's peer of the Code Generator, and a separate agent on purpose: the
generator never reads before it writes (it is create-only by contract), which pointed at somebody
else's repository is a blind clobber. This one drives the shared agentic loop — read, then write the
complete corrected file — with writes confined to the work item's validated targets.

The stub gateway drives the real tools, so the fences are exercised rather than assumed.
"""

from __future__ import annotations

from typing import Any

from app.agents.code_modifier import CodeModifierAgent
from app.graph.router import route_after_change_gate, route_after_modify, route_after_select
from app.integrations.executor import FakeExecutor
from app.models import WorkItem

_FILES = {
    "p1/src/auth/token.py": "def verify(t):\n    return True\n",
    "p1/src/auth/login.py": "from .token import verify\n",
    "p1/README.md": "# app\n",
}

_ITEM = WorkItem(
    id="auth-token-expiry",
    action="modify",
    change_intent="Make verify() reject an expired token.",
    target_files=["src/auth/token.py"],
    acceptance_criteria=["An expired token is rejected"],
)


class _StubLLM:
    """Drives the real tools the way the model does. ``writes`` is a list of (path, content)."""

    def __init__(self, writes: list[tuple[str, str]], *, read_first: bool = True) -> None:
        self._writes = writes
        self._read_first = read_first
        self.prompts: list[str] = []
        self.tool_names: list[str] = []
        self.results: list[str] = []

    def complete_with_tools(self, prompt: str, *, system: str | None = None,
                            tools: list | None = None, max_iters: int = 6) -> str:
        self.prompts.append(prompt)
        by_name = {t.name: t for t in (tools or [])}
        self.tool_names = sorted(by_name)
        for path, content in self._writes:
            if self._read_first:
                by_name["read_file"].handler(path=path)
            self.results.append(str(by_name["write_file"].handler(path=path, content=content)))
        return "Added an expiry check to verify()."


def _state(**overrides: Any) -> dict:
    state = {
        "project_id": "p1", "run_id": "r1", "source_mode": "brownfield",
        "current_work_item": _ITEM, "work_items": [_ITEM],
        "change_request": {"title": "Reject expired tokens",
                           "description": "verify() accepts expired tokens."},
        "generation_summary": "",
    }
    state.update(overrides)
    return state


def _run(llm: _StubLLM, executor: FakeExecutor | None = None, **overrides: Any):
    ex = executor or FakeExecutor(files=dict(_FILES))
    out = CodeModifierAgent(executor=ex, llm=llm).execute(_state(**overrides))
    return out, ex


# --- the edit ------------------------------------------------------------------------------------


def test_an_edit_is_written_and_recorded() -> None:
    new = "def verify(t):\n    return not expired(t)\n"
    out, ex = _run(_StubLLM([("src/auth/token.py", new)]))

    assert ex.files["p1/src/auth/token.py"] == new
    assert out["changed_files"] == ["p1/src/auth/token.py"]
    assert out["generated_code"] == ["p1/src/auth/token.py"]
    assert out["codegen_ok"] is True
    assert "expiry check" in out["modifier_notes"]


def test_untargeted_files_are_left_alone() -> None:
    out, ex = _run(_StubLLM([("src/auth/token.py", "changed\n")]))
    assert ex.files["p1/src/auth/login.py"] == _FILES["p1/src/auth/login.py"]
    assert ex.files["p1/README.md"] == _FILES["p1/README.md"]


def test_a_write_outside_the_work_items_targets_is_refused() -> None:
    # The plan was validated file-by-file; an edit outside it would be an unreviewed change
    # wearing an approved plan's authorization.
    llm = _StubLLM([("README.md", "# hijacked\n")])
    out, ex = _run(llm)

    assert ex.files["p1/README.md"] == _FILES["p1/README.md"]     # untouched
    assert ex.writes == []                                        # the write never happened
    assert out["changed_files"] == []
    assert out["codegen_ok"] is False
    # The refusal goes BACK to the model as the tool result, so it can correct course rather than
    # failing silently (llm_gateway._run_tool feeds a handler's return value straight back).
    assert "REFUSED" in llm.results[0]
    assert "not one of this work item's target files" in llm.results[0]


def test_the_model_gets_read_and_write_only() -> None:
    llm = _StubLLM([("src/auth/token.py", "changed\n")])
    _run(llm)
    assert llm.tool_names == ["read_file", "write_file"]


def test_the_prompt_carries_the_intent_criteria_and_current_content() -> None:
    llm = _StubLLM([("src/auth/token.py", "changed\n")])
    _run(llm)
    prompt = llm.prompts[0]
    assert "Work item: auth-token-expiry" in prompt        # ReplayGateway keys fixtures off this
    assert "Make verify() reject an expired token." in prompt
    assert "An expired token is rejected" in prompt
    assert "def verify" in prompt                          # the file's current content is shown


def test_a_create_target_is_announced_as_missing_not_skipped() -> None:
    # Refactoring SKIPS a file it cannot read (a finding about a nonexistent file is stale). Here a
    # missing target is one the plan explicitly asked to create.
    item = WorkItem(id="new", action="create", change_intent="add it",
                    target_files=["src/auth/new.py"])
    llm = _StubLLM([("src/auth/new.py", "x = 1\n")])
    out, ex = _run(llm, current_work_item=item, work_items=[item])

    assert "does not exist yet; create it" in llm.prompts[0]
    assert ex.files["p1/src/auth/new.py"] == "x = 1\n"
    assert out["codegen_ok"] is True


def test_writing_nothing_is_reported_with_the_models_reason() -> None:
    # The prompt tells the model to decline rather than guess; declining must be legible, not look
    # like a crash.
    out, ex = _run(_StubLLM([]))
    assert out["codegen_ok"] is False
    assert "changed nothing" in out["generation_summary"]
    assert ex.writes == []


def test_a_gateway_failure_stops_the_item_rather_than_crashing() -> None:
    class _Exploding(_StubLLM):
        def complete_with_tools(self, *a: Any, **kw: Any) -> str:
            raise RuntimeError("gateway down")

    out, _ = _run(_Exploding([]))
    assert out["codegen_ok"] is False
    assert "the edit call for 'auth-token-expiry' failed" in out["generation_summary"]


def test_a_truncated_rewrite_is_refused_by_the_shared_guard() -> None:
    # Inherited from app/agents/editing.py: a reply far shorter than the original is truncation,
    # not an edit, and writing it would silently destroy the tail of the file.
    original = "".join(f"line {i}\n" for i in range(400))
    ex = FakeExecutor(files={**_FILES, "p1/src/auth/token.py": original})
    out, _ = _run(_StubLLM([("src/auth/token.py", "line 0\n")]), executor=ex)

    assert ex.files["p1/src/auth/token.py"] == original      # not corrupted
    assert out["changed_files"] == []


# --- retry --------------------------------------------------------------------------------------


def test_a_retry_increments_the_counter_and_shows_the_gate_reasons() -> None:
    failed_gate = {"passed": False, "checks": [
        {"name": "files_changed", "passed": False,
         "stderr": "no change detected in: src/auth/token.py", "stdout": "", "exit_code": 1,
         "scope": ""}]}
    llm = _StubLLM([("src/auth/token.py", "changed\n")])
    out, _ = _run(llm, gate_result=failed_gate, repair_attempt=0)

    assert out["repair_attempt"] == 1
    assert "Your previous attempt was REJECTED" in llm.prompts[0]
    assert "no change detected in: src/auth/token.py" in llm.prompts[0]


def test_a_passing_gate_is_not_treated_as_a_retry() -> None:
    passed_gate = {"passed": True, "checks": []}
    llm = _StubLLM([("src/auth/token.py", "changed\n")])
    out, _ = _run(llm, gate_result=passed_gate, repair_attempt=0)

    assert out.get("repair_attempt", 0) == 0
    assert "REJECTED" not in llm.prompts[0]


# --- routing -------------------------------------------------------------------------------------


def test_select_dispatches_by_mode() -> None:
    # Both lanes share the cursor walk and diverge on what "work an item" means.
    assert route_after_select({"source_mode": "brownfield", "current_work_item": _ITEM}) == "code_modifier"
    assert route_after_select({"current_work_item": _ITEM}) == "code_generator"
    assert route_after_select({"source_mode": "brownfield", "current_work_item": None}) == "change_commit"
    assert route_after_select({"current_work_item": None}) == "commit"


def test_route_after_modify_escalates_an_item_the_model_declined() -> None:
    assert route_after_modify({"codegen_ok": True}) == "change_gate"
    assert route_after_modify({"codegen_ok": False}) == "escalate"
    assert route_after_modify({}) == "change_gate"       # absent == the greenfield default


def test_route_after_change_gate_retries_under_the_cap_then_escalates() -> None:
    passed = {"passed": True, "checks": []}
    failed = {"passed": False, "checks": []}
    assert route_after_change_gate({"gate_result": passed}) == "select"
    assert route_after_change_gate({"gate_result": failed, "repair_attempt": 0}) == "code_modifier"
    assert route_after_change_gate({"gate_result": failed, "repair_attempt": 2}) == "code_modifier"
    assert route_after_change_gate({"gate_result": failed, "repair_attempt": 3}) == "escalate"


def test_the_gate_router_is_pure() -> None:
    # The counter belongs to the node that retries (as repair_node owns it on the greenfield lane);
    # a router that mutated state would double-count on any re-evaluation.
    state = {"gate_result": {"passed": False, "checks": []}, "repair_attempt": 1}
    route_after_change_gate(state)
    assert state["repair_attempt"] == 1
