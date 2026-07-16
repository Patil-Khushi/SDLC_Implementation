"""Foundation checks: settings, BaseAgent contract, and the FakeLLMGateway double."""

import pytest

from app.agents.base import BaseAgent
from app.config.settings import get_settings
from app.graph.state import WorkflowState
from app.services.llm_gateway import FakeLLMGateway


def test_settings_load() -> None:
    settings = get_settings()
    assert settings.app_name
    assert settings.llm_model


def test_fake_gateway_queue_and_records_calls() -> None:
    gateway = FakeLLMGateway(["first", "second"])
    assert gateway.complete("p1") == "first"
    assert gateway.complete("p2", system="sys") == "second"
    assert [c["prompt"] for c in gateway.calls] == ["p1", "p2"]
    assert gateway.calls[1]["system"] == "sys"


def test_fake_gateway_callable_and_default() -> None:
    assert FakeLLMGateway(lambda prompt: prompt.upper()).complete("hi") == "HI"
    assert FakeLLMGateway([], default="D").complete("x") == "D"
    with pytest.raises(IndexError):
        FakeLLMGateway([]).complete("x")


def test_base_agent_execute_contract() -> None:
    class _Echo(BaseAgent):
        name = "echo"

        def execute(self, state: WorkflowState) -> WorkflowState:
            state["generation_summary"] = "ran"
            return state

    out = _Echo().execute({"run_id": "r", "attempt": 0})
    assert out["generation_summary"] == "ran"


def test_load_prompt_reads_markdown() -> None:
    class _A(BaseAgent):
        name = "a"

        def execute(self, state: WorkflowState) -> WorkflowState:
            return state

    assert "Design Package" in _A()._load_prompt("code_generation")
