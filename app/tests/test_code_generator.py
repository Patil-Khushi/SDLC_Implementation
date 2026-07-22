"""Acceptance tests for the Code Generation agent (Prompt 5).

Uses FakeLLMGateway (canned model output) + FakeExecutor (captures writes) — no network, no
sandbox. Covers the two-file success path and the invalid-JSON failure path.
"""

import json
from typing import Any

from app.agents.code_generator import CodeGeneratorAgent
from app.graph.state import WorkflowState, new_state
from app.integrations.executor import FakeExecutor
from app.models import WorkItem
from app.services.llm_gateway import FakeLLMGateway

# A Login backend work item + a matching design pack (artifact bundle).
LOGIN_ITEM = WorkItem(
    id="WI-001",
    requirement_ids=["REQ-1", "REQ-2"],
    endpoints=["POST /login"],
    tables=["users"],
    target_files=["app/api/login.py", "app/services/login_service.py"],
)
DESIGN_PACK = {
    "SKILL.md": "Use snake_case; type hints everywhere.",
    "openapi.yaml": {"paths": {"/login": {"post": {"summary": "Log in"}}}},
    "schema.sql": "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);",
    "validation-rules.json": {"POST /login": {"password": "Password is required."}},
}
TWO_FILE_JSON = json.dumps(
    {
        "files": [
            {"path": "app/api/login.py", "content": "# login controller\n"},
            {"path": "app/services/login_service.py", "content": "# login service\n"},
        ],
        "notes": "",
    }
)


def _state_with_item(item: WorkItem, design_pack: dict[str, Any]) -> WorkflowState:
    state = new_state(run_id="run-1", attempt=2, project_id="p1", design_package=design_pack)
    state["current_work_item"] = item
    return state


def test_two_file_backend_item_is_written_and_recorded() -> None:
    executor = FakeExecutor()
    agent = CodeGeneratorAgent(executor=executor, llm=FakeLLMGateway([TWO_FILE_JSON]))

    out = agent.execute(_state_with_item(LOGIN_ITEM, DESIGN_PACK))

    # both files landed in the workspace and appear in generated_code
    assert out["generated_code"] == ["p1/app/api/login.py", "p1/app/services/login_service.py"]
    assert executor.files["p1/app/api/login.py"] == "# login controller\n"
    assert executor.files["p1/app/services/login_service.py"] == "# login service\n"

    # generation_summary lists the item's covered REQ IDs + endpoint
    summary = out["generation_summary"]
    assert "WI-001" in summary
    assert "REQ-1" in summary and "REQ-2" in summary
    assert "POST /login" in summary

    # a [plan] line (what will be produced + which context sections were used) precedes the
    # [code_generator] outcome line — logged before the LLM is even called
    assert "[plan] WI-001:" in summary
    assert summary.index("[plan]") < summary.index("[code_generator]")
    assert "app/api/login.py" in summary and "app/services/login_service.py" in summary
    assert "context=" in summary and "API" in summary and "DB" in summary

    # metrics: files_produced == 2 (compile/repair fields untouched)
    assert out["generation_metrics"]["files_produced"] == 2
    assert "WI-001" in out["generation_metrics"]["seconds_per_item"]
    assert "compile_passes" not in out["generation_metrics"]
    assert "repairs_used" not in out["generation_metrics"]

    # run_id and attempt echoed unchanged
    assert out["run_id"] == "run-1"
    assert out["attempt"] == 2
    assert out["workflow_status"] == "code_generated"


def test_context_includes_cited_slices() -> None:
    gateway = FakeLLMGateway([TWO_FILE_JSON])
    agent = CodeGeneratorAgent(executor=FakeExecutor(), llm=gateway)

    agent.execute(_state_with_item(LOGIN_ITEM, DESIGN_PACK))

    prompt = gateway.calls[0]["prompt"]
    assert "POST /login" in prompt                 # cited endpoint reached the prompt
    assert "users" in prompt                       # cited table's CREATE TABLE was sliced in
    assert "Password is required." in prompt       # validation message carried verbatim


def test_invalid_json_twice_records_failure_no_writes() -> None:
    executor = FakeExecutor()
    agent = CodeGeneratorAgent(executor=executor, llm=FakeLLMGateway(["not json", "still not json"]))

    item = WorkItem(id="WI-002", requirement_ids=["REQ-9"], endpoints=["POST /x"], target_files=["a.py"])
    out = agent.execute(_state_with_item(item, {}))

    # no files written, no partial state
    assert out["generated_code"] == []
    assert executor.writes == []
    assert out["generation_metrics"].get("files_produced", 0) == 0

    # item recorded as failed
    assert "WI-002" in out["generation_summary"]
    assert "FAILED" in out["generation_summary"]

    # run_id / attempt still unchanged
    assert out["run_id"] == "run-1"
    assert out["attempt"] == 2


def test_invalid_regex_escapes_in_content_are_salvaged() -> None:
    # The backend-root-2 live failure: config/README items carry regex patterns, and the model
    # emits "\." / "\d" inside string values — invalid JSON escapes that json.loads rejects even
    # with strict=False. _extract_json must repair them instead of failing the whole work item
    # (deterministically, on every run, since the same item always carries regexes).
    from app.agents.code_generator import _extract_json

    raw = '{"files":[{"path":".eslintrc.js","content":"rules: [\\"^\\d+$\\", \\"\\.js$\\"]"}],"notes":""}'
    obj = _extract_json(raw)
    assert obj is not None
    assert obj["files"][0]["path"] == ".eslintrc.js"
    assert "\\d" in obj["files"][0]["content"]      # the regex survived, backslash intact
    assert "\\.js" in obj["files"][0]["content"]


def test_valid_escapes_are_not_corrupted_by_the_salvage() -> None:
    from app.agents.code_generator import _extract_json

    raw = json.dumps({"files": [{"path": "a.js", "content": 'line1\nline2\t"quoted" \\ backslash é'}]})
    obj = _extract_json(raw)
    assert obj["files"][0]["content"] == 'line1\nline2\t"quoted" \\ backslash é'


def test_raw_json_reply_with_code_fences_inside_strings_parses_whole_reply() -> None:
    # A README string value containing ``` must not trick the fence regex into extracting a
    # garbage fragment — a reply that starts with '{' is parsed as-is first.
    from app.agents.code_generator import _extract_json

    readme = "# Setup\n```bash\nnpm install\n```\nDone.\n```bash\nnpm test\n```"
    raw = json.dumps({"files": [{"path": "README.md", "content": readme}], "notes": ""})
    obj = _extract_json(raw)
    assert obj is not None
    assert obj["files"][0]["content"] == readme


def test_item_with_regex_heavy_content_completes_end_to_end() -> None:
    executor = FakeExecutor()
    reply = '{"files":[{"path":"knexfile.js","content":"pattern: \\"^\\d{4}\\""}],"notes":""}'
    agent = CodeGeneratorAgent(executor=executor, llm=FakeLLMGateway([reply]))

    item = WorkItem(id="WI-003", requirement_ids=[], target_files=["knexfile.js"])
    out = agent.execute(_state_with_item(item, {}))

    assert out["codegen_ok"] is True
    assert executor.writes == ["p1/knexfile.js"]
    assert "\\d{4}" in executor.files["p1/knexfile.js"]


def test_reask_once_recovers_from_first_bad_reply() -> None:
    executor = FakeExecutor()
    # first reply invalid, second reply valid -> exactly one re-ask, files written
    agent = CodeGeneratorAgent(executor=executor, llm=FakeLLMGateway(["oops not json", TWO_FILE_JSON]))

    out = agent.execute(_state_with_item(LOGIN_ITEM, DESIGN_PACK))
    assert len(out["generated_code"]) == 2
    assert out["generation_metrics"]["files_produced"] == 2


def test_no_current_work_item_is_a_noop() -> None:
    agent = CodeGeneratorAgent(executor=FakeExecutor(), llm=FakeLLMGateway([]))
    state = new_state(run_id="r", attempt=0, project_id="p")
    out = agent.execute(state)   # current_work_item is None
    assert out["generated_code"] == []
    assert out.get("generation_summary", "") == ""
