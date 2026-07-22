"""Documentation Agent tests - pure LLM, no sandbox, no file writes."""

from __future__ import annotations

from app.agents.documentation import DocumentationAgent
from app.integrations.executor import FakeExecutor
from app.services.llm_gateway import FakeLLMGateway


def _state(**over) -> dict:
    base = {"run_id": "r1", "project_id": "proj", "generated_code": []}
    base.update(over)
    return base


def test_documentation_populates_state_from_plain_llm_call() -> None:
    executor = FakeExecutor(files={"proj/app/main.py": "def main():\n    pass\n"})
    llm = FakeLLMGateway(["# My Project\n\nDoes a thing.\n"])

    out = DocumentationAgent(executor=executor, llm=llm).execute(
        _state(generated_code=["proj/app/main.py"])
    )

    assert out["documentation"] == "# My Project\n\nDoes a thing."
    assert len(llm.calls) == 1
    assert "proj/app/main.py" in llm.calls[0]["prompt"]


def test_documentation_never_writes_or_commits() -> None:
    executor = FakeExecutor(files={"proj/app/main.py": "x = 1\n"})
    llm = FakeLLMGateway(["docs"])

    DocumentationAgent(executor=executor, llm=llm).execute(_state(generated_code=["proj/app/main.py"]))

    assert executor.writes == []
    assert executor.commits == []


def test_documentation_handles_empty_generated_code_without_crashing() -> None:
    executor = FakeExecutor()
    llm = FakeLLMGateway(["Nothing to document yet."])

    out = DocumentationAgent(executor=executor, llm=llm).execute(_state())

    assert out["documentation"] == "Nothing to document yet."
    assert "(no source files available)" in llm.calls[0]["prompt"]


def test_documentation_includes_style_guide_when_present() -> None:
    executor = FakeExecutor()
    llm = FakeLLMGateway(["docs"])

    DocumentationAgent(executor=executor, llm=llm).execute(
        _state(design_package={"SKILL.md": "Use snake_case."})
    )

    assert "snake_case" in llm.calls[0]["prompt"]
