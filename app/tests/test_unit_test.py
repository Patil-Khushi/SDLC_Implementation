"""Acceptance tests for the Unit Test agent.

Uses FakeLLMGateway (canned model output) + FakeExecutor (captures writes) — no network, no
sandbox. Mirrors test_code_generator.py's conventions.
"""

import json

from app.agents.unit_test import (
    _PROMPT_HEAD_KEEP,
    _PROMPT_TAIL_KEEP,
    _cap_files_block,
    UnitTestAgent,
)
from app.graph.state import WorkflowState, new_state
from app.integrations.executor import FakeExecutor
from app.models import WorkItem
from app.services.llm_gateway import FakeLLMGateway

LOGIN_ITEM = WorkItem(
    id="WI-001",
    requirement_ids=["REQ-1"],
    endpoints=["POST /login"],
    target_files=["app/api/login.py"],
)
LOGIN_SOURCE = "def login():\n    return True\n"
ONE_TEST_FILE_JSON = json.dumps(
    {
        "files": [
            {"path": "app/api/test_login.py", "content": "def test_login():\n    assert True\n"},
        ],
        "notes": "",
    }
)

SIGNUP_ITEM = WorkItem(
    id="WI-002",
    requirement_ids=["REQ-2"],
    endpoints=["POST /signup"],
    target_files=["app/api/signup.py"],
)
SIGNUP_SOURCE = "def signup():\n    return True\n"
SIGNUP_TEST_FILE_JSON = json.dumps(
    {
        "files": [
            {"path": "app/api/test_signup.py", "content": "def test_signup():\n    assert True\n"},
        ],
        "notes": "",
    }
)


def _state_with_items(*items: WorkItem) -> WorkflowState:
    return new_state(run_id="run-1", attempt=2, project_id="p1", work_items=list(items))


def test_single_item_writes_test_file_and_sets_flags() -> None:
    executor = FakeExecutor(files={"p1/app/api/login.py": LOGIN_SOURCE})
    agent = UnitTestAgent(executor=executor, llm=FakeLLMGateway([ONE_TEST_FILE_JSON]))

    out = agent.execute(_state_with_items(LOGIN_ITEM))

    # test file landed under the project_dir prefix and is recorded in unit_tests
    assert out["unit_tests"] == ["p1/app/api/test_login.py"]
    assert executor.files["p1/app/api/test_login.py"] == "def test_login():\n    assert True\n"
    assert out["tests_ok"] is True

    summary = out["generation_summary"]
    assert "WI-001" in summary
    assert "1 test file(s) written" in summary

    # run_id / attempt echoed unchanged
    assert out["run_id"] == "run-1"
    assert out["attempt"] == 2

    assert out["generation_metrics"]["tests_written"] == 1


def test_partial_failure_still_yields_second_items_success() -> None:
    executor = FakeExecutor(
        files={
            "p1/app/api/login.py": LOGIN_SOURCE,
            "p1/app/api/signup.py": SIGNUP_SOURCE,
        }
    )
    # First item: both the initial ask and the one retry return invalid JSON -> FAILED, 0 files.
    # Second item: valid JSON on the first ask -> succeeds.
    agent = UnitTestAgent(
        executor=executor,
        llm=FakeLLMGateway(["not json", "still not json", SIGNUP_TEST_FILE_JSON]),
    )

    out = agent.execute(_state_with_items(LOGIN_ITEM, SIGNUP_ITEM))

    # partial success: overall tests_ok is True because AT LEAST ONE file was written
    assert out["tests_ok"] is True
    assert out["unit_tests"] == ["p1/app/api/test_signup.py"]

    summary = out["generation_summary"]
    assert "WI-001" in summary and "FAILED" in summary
    assert "WI-002" in summary and "1 test file(s) written" in summary


def test_zero_work_items_is_a_noop_with_tests_ok_true() -> None:
    # An empty plan has nothing to test - this must NOT be confused with "the LLM failed to
    # produce tests for items that existed", which is the real tests_ok=False case below. A
    # zero-work-item run has to stay auto-completable (no human-in-the-loop invariant), not
    # escalate.
    agent = UnitTestAgent(executor=FakeExecutor(), llm=FakeLLMGateway([]))

    out = agent.execute(_state_with_items())

    assert out["unit_tests"] == []
    assert out["tests_ok"] is True


def test_llm_always_failing_leaves_tests_ok_false_and_no_writes() -> None:
    executor = FakeExecutor(files={"p1/app/api/login.py": LOGIN_SOURCE})
    agent = UnitTestAgent(executor=executor, llm=FakeLLMGateway(default="not json"))

    out = agent.execute(_state_with_items(LOGIN_ITEM))

    assert out["unit_tests"] == []
    assert out["tests_ok"] is False
    assert executor.writes == []
    assert "WI-001" in out["generation_summary"] and "FAILED" in out["generation_summary"]


def test_metrics_gain_tests_written_without_disturbing_existing_keys() -> None:
    executor = FakeExecutor(files={"p1/app/api/login.py": LOGIN_SOURCE})
    agent = UnitTestAgent(executor=executor, llm=FakeLLMGateway([ONE_TEST_FILE_JSON]))

    state = _state_with_items(LOGIN_ITEM)
    state["generation_metrics"] = {
        "files_produced": 5,
        "compile_passes": 2,
        "seconds_per_item": {"WI-000": 1.23},
    }

    out = agent.execute(state)

    assert out["generation_metrics"]["tests_written"] == 1
    assert out["generation_metrics"]["files_produced"] == 5
    assert out["generation_metrics"]["compile_passes"] == 2
    assert out["generation_metrics"]["seconds_per_item"] == {"WI-000": 1.23}


# --------------------------------------------------------------------------- U1: runner detection


JEST_BACKEND_ITEM = WorkItem(id="WI-010", target_files=["backend/src/routes/login.js"])
JEST_BACKEND_SOURCE = "module.exports = { login: () => true };\n"
JEST_BACKEND_TEST_JSON = json.dumps(
    {"files": [{"path": "backend/src/routes/login.test.js", "content": "test('login', () => {});\n"}], "notes": ""}
)

VITEST_FRONTEND_ITEM = WorkItem(id="WI-011", target_files=["frontend/src/components/Button.tsx"])
VITEST_FRONTEND_SOURCE = "export function Button() { return null; }\n"
VITEST_FRONTEND_TEST_JSON = json.dumps(
    {"files": [{"path": "frontend/src/components/Button.test.tsx", "content": "test('renders', () => {});\n"}], "notes": ""}
)


def test_runner_fact_uses_jest_when_backend_package_json_declares_it() -> None:
    # The manifest is the ground truth — no need to infer Jest from file shape.
    executor = FakeExecutor(
        files={
            "p1/backend/src/routes/login.js": JEST_BACKEND_SOURCE,
            "p1/backend/package.json": json.dumps({"devDependencies": {"jest": "^29.7.0"}}),
        }
    )
    llm = FakeLLMGateway([JEST_BACKEND_TEST_JSON])
    agent = UnitTestAgent(executor=executor, llm=llm)

    agent.execute(_state_with_items(JEST_BACKEND_ITEM))

    assert "This project's test runner is Jest. Write Jest-dialect tests only." in llm.calls[0]["prompt"]


def test_runner_fact_uses_vitest_when_frontend_package_json_declares_it() -> None:
    executor = FakeExecutor(
        files={
            "p1/frontend/src/components/Button.tsx": VITEST_FRONTEND_SOURCE,
            "p1/frontend/package.json": json.dumps({"devDependencies": {"vitest": "^2.1.0"}}),
        }
    )
    llm = FakeLLMGateway([VITEST_FRONTEND_TEST_JSON])
    agent = UnitTestAgent(executor=executor, llm=llm)

    agent.execute(_state_with_items(VITEST_FRONTEND_ITEM))

    assert "This project's test runner is Vitest. Write Vitest-dialect tests only." in llm.calls[0]["prompt"]


def test_runner_fact_reads_scripts_test_when_devdependencies_is_silent() -> None:
    # jest/vitest can be brought in transitively (e.g. via a test framework meta-package) without
    # appearing as its own devDependency entry — the "test" script is the other fact the manifest
    # states unambiguously.
    executor = FakeExecutor(
        files={
            "p1/backend/src/routes/login.js": JEST_BACKEND_SOURCE,
            "p1/backend/package.json": json.dumps({"devDependencies": {}, "scripts": {"test": "jest --runInBand"}}),
        }
    )
    llm = FakeLLMGateway([JEST_BACKEND_TEST_JSON])
    agent = UnitTestAgent(executor=executor, llm=llm)

    agent.execute(_state_with_items(JEST_BACKEND_ITEM))

    assert "This project's test runner is Jest." in llm.calls[0]["prompt"]


def test_runner_fact_is_honest_when_package_json_declares_neither() -> None:
    executor = FakeExecutor(
        files={
            "p1/backend/src/routes/login.js": JEST_BACKEND_SOURCE,
            "p1/backend/package.json": json.dumps({"devDependencies": {}}),
        }
    )
    llm = FakeLLMGateway([JEST_BACKEND_TEST_JSON])
    agent = UnitTestAgent(executor=executor, llm=llm)

    agent.execute(_state_with_items(JEST_BACKEND_ITEM))

    assert "could not be determined" in llm.calls[0]["prompt"]


def test_runner_fact_is_honest_when_package_json_is_missing() -> None:
    # No package.json at all under backend/ - the read fails, and the runner must stay
    # undetermined rather than silently defaulting to some guess.
    executor = FakeExecutor(files={"p1/backend/src/routes/login.js": JEST_BACKEND_SOURCE})
    llm = FakeLLMGateway([JEST_BACKEND_TEST_JSON])
    agent = UnitTestAgent(executor=executor, llm=llm)

    agent.execute(_state_with_items(JEST_BACKEND_ITEM))

    assert "could not be determined" in llm.calls[0]["prompt"]


def test_python_item_uses_pytest_without_needing_a_package_json() -> None:
    # No package.json exists anywhere in this executor. If the agent incorrectly attempted a
    # manifest lookup for a .py item, read_file would raise and the runner would resolve to the
    # "could not be determined" fallback instead of "pytest" - this pins the short-circuit.
    executor = FakeExecutor(files={"p1/app/api/login.py": LOGIN_SOURCE})
    llm = FakeLLMGateway([ONE_TEST_FILE_JSON])
    agent = UnitTestAgent(executor=executor, llm=llm)

    agent.execute(_state_with_items(LOGIN_ITEM))

    assert "This project's test runner is pytest. Write pytest-dialect tests only." in llm.calls[0]["prompt"]


def test_runner_fact_falls_back_to_project_root_package_json_with_no_backend_frontend_prefix() -> None:
    # A combined/legacy-shaped project has no backend/ or frontend/ wrapper - the manifest lives
    # at the project root instead.
    item = WorkItem(id="WI-012", target_files=["src/server.js"])
    executor = FakeExecutor(
        files={
            "p1/src/server.js": "module.exports = {};\n",
            "p1/package.json": json.dumps({"devDependencies": {"jest": "^29.7.0"}}),
        }
    )
    llm = FakeLLMGateway(
        [json.dumps({"files": [{"path": "src/server.test.js", "content": "test('x', () => {});\n"}], "notes": ""})]
    )
    agent = UnitTestAgent(executor=executor, llm=llm)

    agent.execute(_state_with_items(item))

    assert "This project's test runner is Jest." in llm.calls[0]["prompt"]


# --------------------------------------------------------------------------- U3: skip on empty sources


def test_item_with_no_readable_source_is_skipped_without_calling_the_model() -> None:
    executor = FakeExecutor()  # empty: LOGIN_ITEM's only target file is unreadable
    llm = FakeLLMGateway([])
    agent = UnitTestAgent(executor=executor, llm=llm)

    out = agent.execute(_state_with_items(LOGIN_ITEM))

    assert llm.calls == []  # never asked to hallucinate tests for code it never saw
    assert out["unit_tests"] == []
    assert out["tests_ok"] is False  # a work item existed and yielded no test file
    summary = out["generation_summary"]
    assert "WI-001" in summary and "SKIPPED" in summary and "no readable source files" in summary
    assert "FAILED" not in summary  # distinct failure mode from an unparseable model reply


def test_skipped_item_does_not_count_toward_the_attempted_denominator() -> None:
    # One item has no readable source (skipped, not the model's fault) and one succeeds. The
    # coverage headline should read 1/1 (100%), not 1/2 (50%) - the skip must not make the model
    # look like it failed on an item it was never even asked about.
    executor = FakeExecutor(files={"p1/app/api/signup.py": SIGNUP_SOURCE})
    llm = FakeLLMGateway([SIGNUP_TEST_FILE_JSON])
    agent = UnitTestAgent(executor=executor, llm=llm)

    out = agent.execute(_state_with_items(LOGIN_ITEM, SIGNUP_ITEM))

    summary = out["generation_summary"]
    assert "[unit_test] coverage: 1/1 work item(s) got tests (100%), 1 skipped" in summary


# --------------------------------------------------------------------------- U4: prompt size cap


def test_cap_files_block_leaves_small_blocks_untouched() -> None:
    text = "small content, well under the cap"
    assert _cap_files_block(text) == text


def test_cap_files_block_truncates_and_preserves_head_and_tail() -> None:
    head = "H" * _PROMPT_HEAD_KEEP
    tail = "T" * _PROMPT_TAIL_KEEP
    middle = "M" * 10_000  # pushes well past the cap without touching the kept head/tail regions
    text = head + middle + tail

    capped = _cap_files_block(text)

    assert len(capped) < len(text)
    assert capped.startswith(head)
    assert capped.endswith(tail)
    assert "truncated" in capped
    assert str(len(text)) in capped  # the original size is reported, not silently dropped


def test_build_prompt_caps_an_oversized_source_block() -> None:
    huge = "x" * 60_000
    prompt = UnitTestAgent._build_prompt(LOGIN_ITEM, {"p1/app/api/login.py": huge}, "pytest")

    assert len(prompt) < len(huge)
    assert "truncated" in prompt


# --------------------------------------------------------------------------- U6: dedup counting


SHARED_PATH_TEST_JSON_A = json.dumps(
    {"files": [{"path": "app/api/test_shared.py", "content": "def test_a():\n    assert True\n"}], "notes": ""}
)
SHARED_PATH_TEST_JSON_B = json.dumps(
    {"files": [{"path": "app/api/test_shared.py", "content": "def test_b():\n    assert True\n"}], "notes": ""}
)


def test_same_path_returned_for_two_items_counts_once_toward_tests_written() -> None:
    # Before the fix, `tests_written` counted every WRITE (2), while the de-duplicated `unit_tests`
    # list only ever holds the path once - the metric overcounted relative to the real output.
    executor = FakeExecutor(
        files={
            "p1/app/api/login.py": LOGIN_SOURCE,
            "p1/app/api/signup.py": SIGNUP_SOURCE,
        }
    )
    llm = FakeLLMGateway([SHARED_PATH_TEST_JSON_A, SHARED_PATH_TEST_JSON_B])
    agent = UnitTestAgent(executor=executor, llm=llm)

    out = agent.execute(_state_with_items(LOGIN_ITEM, SIGNUP_ITEM))

    assert out["unit_tests"] == ["p1/app/api/test_shared.py"]
    assert out["generation_metrics"]["tests_written"] == 1
