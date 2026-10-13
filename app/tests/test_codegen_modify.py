"""The Code Generator editing code it already generated.

Until now this agent could only CREATE: it calls ``self.llm.complete``, expects strict JSON
``{"files":[...]}``, and never reads a file. That is why a changed requirement meant rebuilding the
project — there was no code path capable of looking at what already existed.

Modify mode is the other half. These tests pin the three things that make it safe to point at code
a run has already committed and pushed:

* it READS before it writes (a blind whole-file write discards work other items depend on),
* the ``allowed_paths`` fence refuses a write outside the work item's declared targets, and
* an ``action="create"`` item is untouched by any of it.

``FakeLLMGateway.complete_with_tools`` deliberately ignores the tools it is handed, so it cannot
exercise a tool loop. The double here drives the tools for real, which is the only way to prove the
fence actually refuses.
"""

from __future__ import annotations

import json
from typing import Any

from app.agents.code_generator import CodeGeneratorAgent
from app.graph.state import WorkflowState, new_state
from app.integrations.executor import FakeExecutor
from app.models import WorkItem
from app.services import requirement_ledger
from app.services.llm_gateway import FakeLLMGateway, LLMGateway

PROJECT = "p1"


class ToolDrivingLLM(LLMGateway):
    """A gateway double that actually invokes the tools it is given.

    ``actions`` is a list of ``(tool_name, kwargs)`` the "model" performs in order. Every tool
    result is captured in :attr:`results` so a test can assert on a refusal string — the refusal is
    the model's instruction, so it has to be observable.
    """

    def __init__(self, actions: list[tuple[str, dict[str, Any]]], notes: str = "done") -> None:
        self.actions = actions
        self.notes = notes
        self.results: list[str] = []
        self.complete_calls: list[str] = []
        self.tool_calls: list[str] = []

    def complete(self, prompt: str, *, system: str | None = None, **kwargs: Any) -> str:
        self.complete_calls.append(prompt)
        raise AssertionError("modify mode must not use the strict-JSON complete() path")

    def complete_with_tools(
        self, prompt: str, *, system: str | None = None,
        tools: list | None = None, max_iters: int = 6,
    ) -> str:
        self.tool_calls.append(prompt)
        by_name = {t.name: t for t in (tools or [])}
        for name, kwargs in self.actions:
            self.results.append(str(by_name[name].handler(**kwargs)))
        return self.notes


def _existing_project(files: dict[str, str]) -> FakeExecutor:
    return FakeExecutor(files={f"{PROJECT}/{rel}": body for rel, body in files.items()})


def _state(item: WorkItem) -> WorkflowState:
    state = new_state(run_id="run-1", attempt=0, project_id=PROJECT, design_package={})
    state["current_work_item"] = item
    return state


_MODIFY_ITEM = WorkItem(
    id="WI-001-m1",
    feature_title="Login",
    requirement_ids=["REQ-1"],
    target_files=["backend/login.py"],
    action="modify",
    change_intent="Use a one-time password instead of a static password.",
    acceptance_criteria=["POST /login accepts an `otp` field"],
)


def test_modify_reads_the_existing_file_then_edits_it_in_place() -> None:
    executor = _existing_project({"backend/login.py": "def login(password):\n    ...\n"})
    llm = ToolDrivingLLM(
        [
            ("read_file", {"path": "backend/login.py"}),
            ("write_file", {"path": "backend/login.py", "content": "def login(otp):\n    ...\n"}),
        ],
        notes="switched login to OTP",
    )
    agent = CodeGeneratorAgent(executor=executor, llm=llm)

    out = agent.execute(_state(_MODIFY_ITEM))

    assert out["codegen_ok"] is True
    assert executor.files[f"{PROJECT}/backend/login.py"] == "def login(otp):\n    ...\n"
    # the read happened, and it returned the ORIGINAL content
    assert "def login(password)" in llm.results[0]
    assert f"{PROJECT}/backend/login.py" in out["generated_code"]


def test_modify_uses_the_tool_loop_and_never_the_strict_json_path() -> None:
    executor = _existing_project({"backend/login.py": "x\n"})
    llm = ToolDrivingLLM([("write_file", {"path": "backend/login.py", "content": "y\n"})])
    CodeGeneratorAgent(executor=executor, llm=llm).execute(_state(_MODIFY_ITEM))

    assert len(llm.tool_calls) == 1
    assert llm.complete_calls == [], "complete() would have raised; assert it was never reached"


def test_the_prompt_carries_the_intent_the_criteria_and_the_editable_files() -> None:
    executor = _existing_project({"backend/login.py": "x\n"})
    llm = ToolDrivingLLM([("write_file", {"path": "backend/login.py", "content": "y\n"})])
    CodeGeneratorAgent(executor=executor, llm=llm).execute(_state(_MODIFY_ITEM))

    prompt = llm.tool_calls[0]
    assert "one-time password" in prompt
    assert "accepts an `otp` field" in prompt
    assert "backend/login.py" in prompt
    assert "refused" in prompt.lower(), "the model must be told the fence exists"


def test_a_write_outside_the_target_files_is_refused_and_the_file_is_untouched() -> None:
    # The fence is the only thing between a conversational request and an unreviewed edit.
    executor = _existing_project(
        {"backend/login.py": "x\n", "backend/secrets.py": "SECRET = 1\n"}
    )
    llm = ToolDrivingLLM([
        ("write_file", {"path": "backend/secrets.py", "content": "SECRET = 2\n"}),
        ("write_file", {"path": "backend/login.py", "content": "y\n"}),
    ])
    agent = CodeGeneratorAgent(executor=executor, llm=llm)

    out = agent.execute(_state(_MODIFY_ITEM))

    assert executor.files[f"{PROJECT}/backend/secrets.py"] == "SECRET = 1\n", "must be untouched"
    assert llm.results[0].startswith("REFUSED")
    assert "backend/login.py" in llm.results[0], "the refusal names what MAY be edited"
    # the in-plan write still went through
    assert executor.files[f"{PROJECT}/backend/login.py"] == "y\n"
    assert f"{PROJECT}/backend/secrets.py" not in out["generated_code"]


def test_a_noop_modify_stays_codegen_ok_so_the_gate_decides() -> None:
    # Escalating here would skip the gate's digest check and its repair loop. The gate is what
    # proves a modify item moved its targets; this agent must not pre-empt it.
    executor = _existing_project({"backend/login.py": "x\n"})
    llm = ToolDrivingLLM([("read_file", {"path": "backend/login.py"})], notes="nothing to do")
    out = CodeGeneratorAgent(executor=executor, llm=llm).execute(_state(_MODIFY_ITEM))

    assert out["codegen_ok"] is True
    assert executor.files[f"{PROJECT}/backend/login.py"] == "x\n"


def test_a_failing_model_call_is_a_generation_failure() -> None:
    class Boom(LLMGateway):
        def __init__(self) -> None:
            pass

        def complete(self, prompt: str, *, system: str | None = None, **kwargs: Any) -> str:
            raise AssertionError("unused")

        def complete_with_tools(self, prompt: str, **kwargs: Any) -> str:
            raise RuntimeError("gateway exploded")

    out = CodeGeneratorAgent(executor=_existing_project({"backend/login.py": "x\n"}), llm=Boom()).execute(
        _state(_MODIFY_ITEM)
    )
    assert out["codegen_ok"] is False, "a gateway failure escalates; a no-op edit does not"


def test_delete_action_is_treated_as_an_unwire_edit() -> None:
    # Hard deletion is deliberately unimplemented (no Executor.delete_file), so a delete item takes
    # the edit path and leaves the file importable.
    item = _MODIFY_ITEM.model_copy(update={"action": "delete", "change_intent": "remove wishlist"})
    executor = _existing_project({"backend/login.py": "x\n"})
    llm = ToolDrivingLLM([("write_file", {"path": "backend/login.py", "content": "# removed\n"})])
    out = CodeGeneratorAgent(executor=executor, llm=llm).execute(_state(item))

    assert out["codegen_ok"] is True
    assert executor.files[f"{PROJECT}/backend/login.py"] == "# removed\n"


# --------------------------------------------------------------- the ledger

def test_a_successful_item_records_observed_files_in_the_ledger() -> None:
    executor = _existing_project({"backend/login.py": "x\n"})
    llm = ToolDrivingLLM([("write_file", {"path": "backend/login.py", "content": "y\n"})])
    out = CodeGeneratorAgent(executor=executor, llm=llm).execute(_state(_MODIFY_ITEM))

    led = out["requirement_ledger"]
    assert requirement_ledger.is_generated(led, "WI-001-m1")
    # project-RELATIVE, so it drops straight into a follow-up item's target_files
    assert requirement_ledger.files_for_work_item(led, "WI-001-m1") == ["backend/login.py"]
    assert requirement_ledger.files_for_requirements(led, ["REQ-1"]) == ["backend/login.py"]


def test_the_create_path_also_records_the_ledger_and_is_otherwise_unchanged() -> None:
    # Backward compatibility: a create item must take the strict-JSON path exactly as before.
    item = WorkItem(id="WI-002", requirement_ids=["REQ-9"], target_files=["backend/new.py"])
    reply = json.dumps({"files": [{"path": "backend/new.py", "content": "# new\n"}], "notes": ""})
    executor = FakeExecutor()
    llm = FakeLLMGateway([reply])

    out = CodeGeneratorAgent(executor=executor, llm=llm).execute(_state(item))

    assert out["codegen_ok"] is True
    assert executor.files[f"{PROJECT}/backend/new.py"] == "# new\n"
    assert len(llm.calls) == 1, "one strict-JSON call, the original single-target path"
    assert requirement_ledger.files_for_work_item(out["requirement_ledger"], "WI-002") == [
        "backend/new.py"
    ]


def test_a_noop_modify_records_NO_ledger_row() -> None:
    # A row for an item that wrote nothing would make is_generated() True for it, and a later
    # conversational modify of that item would then resolve to zero files and be skipped as
    # "the ledger records no files for it" — an unfixable dead end.
    executor = _existing_project({"backend/login.py": "x\n"})
    llm = ToolDrivingLLM([("read_file", {"path": "backend/login.py"})], notes="nothing to do")
    out = CodeGeneratorAgent(executor=executor, llm=llm).execute(_state(_MODIFY_ITEM))

    assert out["requirement_ledger"] == []
    assert requirement_ledger.is_generated(out["requirement_ledger"], "WI-001-m1") is False


def test_repairing_the_same_item_replaces_its_ledger_row() -> None:
    # The gate/repair loop re-runs an item; two rows would report a stale file set.
    item = WorkItem(id="WI-003", target_files=["backend/a.py"])
    executor = FakeExecutor()
    state = _state(item)

    for body in ("# first\n", "# second\n"):
        reply = json.dumps({"files": [{"path": "backend/a.py", "content": body}], "notes": ""})
        state = CodeGeneratorAgent(executor=executor, llm=FakeLLMGateway([reply])).execute(state)

    rows = [r for r in state["requirement_ledger"] if r["work_item_id"] == "WI-003"]
    assert len(rows) == 1
    assert rows[0]["files"] == ["backend/a.py"]
