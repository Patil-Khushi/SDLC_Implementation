"""Dry-run mode has to answer the prompt the generator actually sends.

``code_generator._generate_files`` routes a work item with **two or more** targets down the per-file
path, which asks for one named file at a time and never prints the "Target files (produce ONLY
these)" block. The canned stand-ins matched only that block, so every per-file call answered with
``placeholder.py``, no reply matched its requested path, and the item failed the gate with "0 of N
planned files".

Every work item in the sample design pack has four targets, so the effect was that a dry-run
escalated at work item 1 — the mode exists precisely to exercise the wiring without an API key, and
it could not do that. These tests pin both prompt shapes so the two copies of the stub cannot drift
back.
"""

from __future__ import annotations

import json

import pytest

from app.agents.code_generator import CodeGeneratorAgent
from app.graph.state import new_state
from app.integrations.executor import FakeExecutor
from app.models import WorkItem
from app.services.llm_gateway import LLMGateway

gs = pytest.importorskip("scripts.graph_server")
rf = pytest.importorskip("scripts.run_fixture")

# The two independent copies of the stub. Both must behave identically — graph_server's docstring
# claims "identical shape to run_fixture.py", and that claim is what these parametrise.
STUBS = [
    pytest.param(gs._canned_llm_reply, id="graph_server"),
    pytest.param(rf._canned_llm_reply, id="run_fixture"),
]

_PER_FILE_PROMPT = (
    "Work item: backend-modules-auth\n"
    "All files planned for this work item (stay consistent with these siblings):\n"
    "- backend/src/modules/auth/auth.routes.js\n"
    "- backend/src/modules/auth/auth.controller.js\n\n"
    "Generate EXACTLY ONE file now: backend/src/modules/auth/auth.controller.js\n"
    'Respond with STRICT JSON containing ONLY that one file: {"files":[{"path":...,"content":...}]}.'
)

_WHOLE_ITEM_PROMPT = (
    "Work item: WI-001\n"
    "Target files (produce ONLY these):\n"
    "- app/api/login.py\n"
    "- app/services/login_service.py\n\n"
    "Respond with STRICT JSON."
)


@pytest.mark.parametrize("stub", STUBS)
def test_the_per_file_prompt_gets_back_the_exact_file_it_asked_for(stub):
    reply = json.loads(stub(_PER_FILE_PROMPT))
    assert [f["path"] for f in reply["files"]] == [
        "backend/src/modules/auth/auth.controller.js"
    ], "the reply must name the requested path, or _parse discards it"
    assert reply["files"][0]["content"].strip()


@pytest.mark.parametrize("stub", STUBS)
def test_the_per_file_prompt_never_falls_through_to_placeholder(stub):
    # The regression: "placeholder.py" here is what made every per-file item produce 0 files.
    reply = json.loads(stub(_PER_FILE_PROMPT))
    assert "placeholder.py" not in [f["path"] for f in reply["files"]]


@pytest.mark.parametrize("stub", STUBS)
def test_the_whole_item_prompt_still_gets_every_target(stub):
    reply = json.loads(stub(_WHOLE_ITEM_PROMPT))
    assert [f["path"] for f in reply["files"]] == [
        "app/api/login.py",
        "app/services/login_service.py",
    ]


@pytest.mark.parametrize("stub", STUBS)
def test_an_unrecognized_prompt_still_returns_parseable_json(stub):
    reply = json.loads(stub("something else entirely"))
    assert reply["files"], "a reply with no files at all would crash _parse"


@pytest.mark.parametrize("stub", STUBS)
def test_a_multi_target_item_generates_every_file_end_to_end(stub):
    """The actual failure, reproduced through the agent: 4 targets, 4 files written."""
    item = WorkItem(
        id="backend-modules-auth",
        target_files=[
            "backend/src/modules/auth/auth.routes.js",
            "backend/src/modules/auth/auth.controller.js",
            "backend/src/modules/auth/auth.service.js",
            "backend/src/modules/auth/auth.validator.js",
        ],
    )

    class Canned(LLMGateway):
        def __init__(self) -> None:
            pass

        def complete(self, prompt: str, *, system: str | None = None, **kw: object) -> str:
            return stub(prompt)

        def complete_with_tools(self, prompt: str, **kw: object) -> str:
            return stub(prompt)

    executor = FakeExecutor()
    state = new_state(run_id="r", attempt=0, project_id="p1", design_package={})
    state["current_work_item"] = item

    out = CodeGeneratorAgent(executor=executor, llm=Canned()).execute(state)

    assert out["codegen_ok"] is True, "this was False — the run escalated at work item 1"
    written = sorted(p.split("p1/", 1)[-1] for p in out["generated_code"])
    assert written == sorted(item.target_files), "all four planned files must land"
