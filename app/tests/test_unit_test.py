"""Acceptance tests for the Unit Test agent.

Uses FakeLLMGateway (canned model output) + FakeExecutor (captures writes) — no network, no
sandbox. Mirrors test_code_generator.py's conventions.
"""

import json

from app.agents.unit_test import UnitTestAgent
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
