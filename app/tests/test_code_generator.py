"""Acceptance tests for the Code Generation agent (Prompt 5).

Uses FakeLLMGateway (canned model output) + FakeExecutor (captures writes) — no network, no
sandbox. Covers the per-file generation path (a multi-file item is generated one file per call so
its output can't grow with the file count and truncate at the max_tokens cap), the single-file
one-shot path, and the invalid-JSON failure path.
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


def _one_file(path: str, content: str) -> str:
    """A valid single-file reply — the shape the per-file path expects from each call."""
    return json.dumps({"files": [{"path": path, "content": content}], "notes": ""})


# LOGIN_ITEM has two targets, so the agent now makes one call per file (in target order): the
# controller first, then the service.
LOGIN_CONTROLLER_JSON = _one_file("app/api/login.py", "# login controller\n")
LOGIN_SERVICE_JSON = _one_file("app/services/login_service.py", "# login service\n")


def _state_with_item(item: WorkItem, design_pack: dict[str, Any]) -> WorkflowState:
    state = new_state(run_id="run-1", attempt=2, project_id="p1", design_package=design_pack)
    state["current_work_item"] = item
    return state


def test_two_file_backend_item_is_written_and_recorded() -> None:
    executor = FakeExecutor()
    # One reply per file (per-file generation): controller, then service.
    agent = CodeGeneratorAgent(
        executor=executor, llm=FakeLLMGateway([LOGIN_CONTROLLER_JSON, LOGIN_SERVICE_JSON])
    )

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
    gateway = FakeLLMGateway([LOGIN_CONTROLLER_JSON, LOGIN_SERVICE_JSON])
    agent = CodeGeneratorAgent(executor=FakeExecutor(), llm=gateway)

    agent.execute(_state_with_item(LOGIN_ITEM, DESIGN_PACK))

    # The shared design-pack context is grounding for every per-file call, so the first file's
    # prompt already carries the cited endpoint/table/validation slices.
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


def test_well_formed_reply_never_needs_the_salvage() -> None:
    # Happy path: json.dumps output parses on the FIRST attempt (the salvage line is not
    # reached at all) and content round-trips exactly.
    from app.agents.code_generator import _extract_json

    raw = json.dumps({"files": [{"path": "a.js", "content": 'line1\nline2\t"quoted" \\ backslash é'}]})
    obj = _extract_json(raw)
    assert obj["files"][0]["content"] == 'line1\nline2\t"quoted" \\ backslash é'


def test_salvage_preserves_valid_escapes_mixed_with_invalid_ones() -> None:
    # PR #15 review (blocking): the original lookahead salvage corrupted an already-correct
    # \\d when a genuinely broken \.js sat in the SAME string — the engine skipped the valid
    # pair's first backslash, then matched its second backslash alone and doubled it
    # (\\d -> \\\d), so the salvaged candidate STILL failed to parse. The token-consuming
    # repair must fix the broken escape while leaving the valid one untouched. This input is
    # invalid JSON, so — unlike a json.dumps round-trip — it genuinely exercises the salvage.
    from app.agents.code_generator import _extract_json

    raw = r'{"files":[{"path":".eslintrc.js","content":"valid: \\d bad: \.js"}],"notes":""}'
    obj = _extract_json(raw)
    assert obj is not None, "salvage failed to recover the reply"
    assert obj["files"][0]["content"] == r"valid: \d bad: \.js"   # both escapes correct, nothing doubled


def test_repair_helper_is_byte_exact_on_every_escape_kind() -> None:
    # Unit-level pin of _repair_invalid_escapes itself: every valid JSON escape consumed
    # untouched, every invalid one doubled — including a valid pair ADJACENT to an invalid one.
    from app.agents.code_generator import _repair_invalid_escapes

    assert _repair_invalid_escapes(r"\d") == r"\\d"                     # invalid -> doubled
    assert _repair_invalid_escapes(r"\\d") == r"\\d"                    # valid pair untouched
    assert _repair_invalid_escapes(r"\\\d") == r"\\\\d"                 # valid pair + invalid tail
    assert _repair_invalid_escapes(r"\n\t\"\\\/ \b\f\r") == r"\n\t\"\\\/ \b\f\r"  # all valid, untouched
    assert _repair_invalid_escapes("\\u0041") == "\\u0041"              # valid unicode escape untouched
    assert _repair_invalid_escapes("\\u12") == "\\\\u12"                # truncated unicode -> doubled
    assert _repair_invalid_escapes("no backslashes") == "no backslashes"


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
    # The first file's first reply is invalid, so it re-asks once (recovering), then the second
    # file's reply is valid first try -> both files written.
    agent = CodeGeneratorAgent(
        executor=executor,
        llm=FakeLLMGateway(["oops not json", LOGIN_CONTROLLER_JSON, LOGIN_SERVICE_JSON]),
    )

    out = agent.execute(_state_with_item(LOGIN_ITEM, DESIGN_PACK))
    assert len(out["generated_code"]) == 2
    assert out["generation_metrics"]["files_produced"] == 2


def test_no_current_work_item_is_a_noop() -> None:
    agent = CodeGeneratorAgent(executor=FakeExecutor(), llm=FakeLLMGateway([]))
    state = new_state(run_id="r", attempt=0, project_id="p")
    out = agent.execute(state)   # current_work_item is None
    assert out["generated_code"] == []
    assert out.get("generation_summary", "") == ""


# --------------------------------------------------------------------------- per-file generation
#
# The truncation fix: a multi-file item is generated ONE FILE PER CALL (bounded output) instead of
# one giant reply carrying every file, which was overrunning the max_tokens cap and coming back as
# truncated, unparseable JSON ("no JSON object found in reply").

CATALOGUE_ITEM = WorkItem(
    id="WI-CAT",
    requirement_ids=["REQ-CAT"],
    target_files=[
        "src/modules/catalogue/catalogue.routes.js",
        "src/modules/catalogue/catalogue.controller.js",
        "src/modules/catalogue/catalogue.service.js",
    ],
)


def test_multi_file_item_generates_one_file_per_call() -> None:
    executor = FakeExecutor()
    gateway = FakeLLMGateway([
        _one_file("src/modules/catalogue/catalogue.routes.js", "// routes\n"),
        _one_file("src/modules/catalogue/catalogue.controller.js", "// controller\n"),
        _one_file("src/modules/catalogue/catalogue.service.js", "// service\n"),
    ])
    agent = CodeGeneratorAgent(executor=executor, llm=gateway)

    out = agent.execute(_state_with_item(CATALOGUE_ITEM, {}))

    assert len(gateway.calls) == 3                       # exactly one call per target file
    assert out["codegen_ok"] is True
    assert out["generation_metrics"]["files_produced"] == 3
    assert executor.writes == [
        "p1/src/modules/catalogue/catalogue.routes.js",
        "p1/src/modules/catalogue/catalogue.controller.js",
        "p1/src/modules/catalogue/catalogue.service.js",
    ]
    # each call is scoped to exactly one file, in target order
    assert all("Generate EXACTLY ONE file now:" in c["prompt"] for c in gateway.calls)
    assert "catalogue.routes.js" in gateway.calls[0]["prompt"]
    assert "catalogue.service.js" in gateway.calls[2]["prompt"]


def test_one_unparseable_file_does_not_sink_the_others() -> None:
    # A single file that never parses is skipped (the completeness gate + repair loop fills it in);
    # the files that DID parse are still written, instead of failing the whole item.
    executor = FakeExecutor()
    gateway = FakeLLMGateway([
        LOGIN_CONTROLLER_JSON,          # file 1 -> ok
        "not json", "still not json",   # file 2 -> bad reply, retry also bad
    ])
    agent = CodeGeneratorAgent(executor=executor, llm=gateway)

    out = agent.execute(_state_with_item(LOGIN_ITEM, DESIGN_PACK))

    assert out["codegen_ok"] is True                                # partial success, not a hard fail
    assert executor.writes == ["p1/app/api/login.py"]               # only the file that parsed
    assert "p1/app/services/login_service.py" not in executor.files
    assert out["generation_metrics"]["files_produced"] == 1


def test_all_files_failing_records_item_failure() -> None:
    # If EVERY file fails to parse, the item is recorded as failed with no writes — same escalation
    # contract as before (codegen_ok False, so the router escalates without a gate/commit).
    executor = FakeExecutor()
    gateway = FakeLLMGateway(default="not json ever")       # every call (and retry) is unparseable
    agent = CodeGeneratorAgent(executor=executor, llm=gateway)

    out = agent.execute(_state_with_item(LOGIN_ITEM, DESIGN_PACK))

    assert out["codegen_ok"] is False
    assert executor.writes == []
    assert out["generated_code"] == []
    assert "FAILED" in out["generation_summary"]


def test_earlier_file_content_is_fed_into_later_file_prompts() -> None:
    # Cross-file coherence: a file generated earlier this item is injected into the prompts that
    # follow, so a later file can import the exact symbols an earlier one defined.
    executor = FakeExecutor()
    gateway = FakeLLMGateway([
        _one_file("app/api/login.py", "# CONTROLLER_MARKER_XYZ\n"),
        _one_file("app/services/login_service.py", "# service\n"),
    ])
    agent = CodeGeneratorAgent(executor=executor, llm=gateway)

    agent.execute(_state_with_item(LOGIN_ITEM, DESIGN_PACK))

    second_prompt = gateway.calls[1]["prompt"]              # the second file's prompt...
    assert "app/api/login.py" in second_prompt              # ...shows the first file's path...
    assert "CONTROLLER_MARKER_XYZ" in second_prompt         # ...and its content
    assert "CONTROLLER_MARKER_XYZ" not in gateway.calls[0]["prompt"]  # first file had no siblings yet


def test_wrong_file_returned_is_rejected_not_relabeled() -> None:
    # If a per-file call returns a DIFFERENT file than requested, its content must NOT be relabeled
    # onto the requested path (that would silently write the wrong code under it). It's skipped, so
    # the gate/repair loop supplies the real file — and the good file is never overwritten.
    executor = FakeExecutor()
    gateway = FakeLLMGateway([
        LOGIN_CONTROLLER_JSON,                            # file 1 (login.py) -> ok
        _one_file("app/api/login.py", "# WRONG FILE\n"),  # file 2 asked for the service, got login.py
    ])
    agent = CodeGeneratorAgent(executor=executor, llm=gateway)

    out = agent.execute(_state_with_item(LOGIN_ITEM, DESIGN_PACK))

    assert executor.writes == ["p1/app/api/login.py"]                       # service.py skipped
    assert executor.files["p1/app/api/login.py"] == "# login controller\n"  # not overwritten
    assert out["generation_metrics"]["files_produced"] == 1
