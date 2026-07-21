"""Refactoring agent: apply the fixes the code review named, written where the next agent looks.

The agent reads the review's structured findings (``review_findings_path`` JSON, or the JSON block
embedded in ``review_report`` as a fallback), asks the LLM for corrected file content per file, and
writes it back under the SAME ``<project_dir>/`` prefix the code generator / repair path use — so
the downstream Debugging agent reads the fix where it expects it. It never commits or runs a gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agents.refactoring import RefactoringAgent
from app.integrations.executor import FakeExecutor


class _StubLLM:
    """Minimal gateway stand-in: returns one canned fix, records the prompts it saw."""

    def __init__(self, content: str = "print(1)\n", path: str = "src/foo.py") -> None:
        self._content = content
        self._path = path
        self.prompts: list[str] = []

    def complete_with_tools(self, prompt: str, *, system: str | None = None,
                            tools: list | None = None, max_iters: int = 4) -> str:
        self.prompts.append(prompt)
        return json.dumps({"files": [{"path": self._path, "content": self._content}], "notes": "x"})


def _findings_file(tmp_path: Path, findings: list[dict[str, Any]]) -> str:
    p = tmp_path / "findings.json"
    p.write_text(json.dumps(findings), encoding="utf-8")
    return str(p)


def _state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"run_id": "r1", "project_id": "proj", "generated_code": []}
    base.update(over)
    return base


def test_applies_fix_from_findings_json(tmp_path: Path) -> None:
    executor = FakeExecutor(files={"proj/src/foo.py": "print(0)\n"})
    findings = [{"file": "src/foo.py", "line": 1, "severity": "High",
                 "category": "Bug", "rule_id": "B001", "message": "wrong literal"}]
    state = _state(review_findings_path=_findings_file(tmp_path, findings))

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(state)

    assert executor.files["proj/src/foo.py"] == "print(1)\n"      # written where the next agent looks
    assert "proj/src/foo.py" in state["generated_code"]           # recorded for the downstream read
    assert state["workflow_status"] == "refactored"


def test_prompt_carries_the_findings(tmp_path: Path) -> None:
    executor = FakeExecutor(files={"proj/src/foo.py": "print(0)\n"})
    findings = [{"file": "src/foo.py", "line": 7, "severity": "High",
                 "category": "Bug", "rule_id": "B001", "message": "unique-finding-message"}]
    llm = _StubLLM()
    RefactoringAgent(executor=executor, llm=llm).execute(
        _state(review_findings_path=_findings_file(tmp_path, findings))
    )

    assert llm.prompts and "unique-finding-message" in llm.prompts[0]
    assert "src/foo.py" in llm.prompts[0]


def test_does_not_double_prefix_an_already_prefixed_path(tmp_path: Path) -> None:
    executor = FakeExecutor(files={"proj/src/foo.py": "print(0)\n"})
    findings = [{"file": "src/foo.py", "severity": "Low", "message": "x"}]
    # A model that echoes the already-prefixed path must not be re-prefixed into proj/proj/....
    RefactoringAgent(executor=executor, llm=_StubLLM(path="proj/src/foo.py")).execute(
        _state(review_findings_path=_findings_file(tmp_path, findings))
    )

    assert "proj/src/foo.py" in executor.files
    assert "proj/proj/src/foo.py" not in executor.files


def test_skips_file_not_in_workspace(tmp_path: Path) -> None:
    executor = FakeExecutor()  # empty workspace
    findings = [{"file": "src/missing.py", "severity": "High", "message": "x"}]
    state = _state(review_findings_path=_findings_file(tmp_path, findings))

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(state)

    assert executor.writes == []                                   # wrote nothing
    assert state["workflow_status"] == "refactored"
    assert "not found" in state["refactored_code"]


def test_no_actionable_findings_stamps_status(tmp_path: Path) -> None:
    executor = FakeExecutor(files={"proj/src/foo.py": "print(0)\n"})
    # Only a project-level finding (no file) — nothing file-scoped to apply.
    findings = [{"file": "", "severity": "Info", "message": "overall looks fine"}]
    state = _state(review_findings_path=_findings_file(tmp_path, findings))

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(state)

    assert executor.writes == []
    assert state["workflow_status"] == "refactored"
    assert "No file-scoped" in state["refactored_code"]


def test_falls_back_to_report_json_block() -> None:
    executor = FakeExecutor(files={"proj/src/foo.py": "print(0)\n"})
    findings = [{"file": "src/foo.py", "severity": "High", "message": "bad"}]
    report = "# Code Review\n\nActionable findings:\n\n```json\n" + json.dumps(findings) + "\n```\n"
    # No review_findings_path — the agent must recover the findings from the report's JSON block.
    state = _state(review_report=report)

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(state)

    assert executor.files["proj/src/foo.py"] == "print(1)\n"
    assert state["workflow_status"] == "refactored"


def test_bad_llm_reply_writes_nothing(tmp_path: Path) -> None:
    executor = FakeExecutor(files={"proj/src/foo.py": "print(0)\n"})
    findings = [{"file": "src/foo.py", "severity": "High", "message": "bad"}]

    class _JunkLLM:
        def complete_with_tools(self, prompt: str, *, system: str | None = None,
                                tools: list | None = None, max_iters: int = 4) -> str:
            return "sorry, I can't help with that"

    state = _state(review_findings_path=_findings_file(tmp_path, findings))
    RefactoringAgent(executor=executor, llm=_JunkLLM()).execute(state)

    assert executor.files["proj/src/foo.py"] == "print(0)\n"       # unchanged, no partial garbage
    assert executor.writes == []
    assert state["workflow_status"] == "refactored"
