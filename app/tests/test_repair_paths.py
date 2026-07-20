"""Repair-path regression: the fix must be written where the completeness gate looks.

The gate checks ``<project_dir>/<target_file>`` and the code_generator writes with that prefix,
but the repair agent used to write the LLM's raw (bare) path. A repair then wrote ``foo.py`` while
the gate re-checked ``<project_dir>/foo.py`` → still "missing" → the loop burned to the cap and
escalated without ever satisfying the gate it exists to satisfy. These tests pin that a repair
write lands under ``<project_dir>/`` (and passes the gate), and that an already-prefixed proposal
is not double-prefixed.
"""

from __future__ import annotations

from typing import Any

from app.agents.repair import RepairAgent
from app.integrations.executor import FakeExecutor


class _FixedReplyLLM:
    """Minimal gateway stand-in: returns one canned repair proposal, ignores the tools."""

    def __init__(self, path: str) -> None:
        self._path = path

    def complete_with_tools(self, prompt: str, *, system: str | None = None,
                            tools: list | None = None, max_iters: int = 4) -> str:
        return f'{{"files":[{{"path":"{self._path}","content":"print(1)"}}],"notes":"x"}}'


def _state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": "r1",
        "project_id": "proj",
        "generated_code": [],
        "gate_result": {"checks": [
            {"name": "files_complete", "passed": False,
             "stderr": "missing required files: backend/app/main.py"},
        ]},
    }
    base.update(over)
    return base


def test_repair_writes_fix_under_project_dir() -> None:
    executor = FakeExecutor()
    # The model echoes the BARE path it saw in the gate's "missing required files" message.
    RepairAgent(executor=executor, llm=_FixedReplyLLM("backend/app/main.py")).execute(_state())

    assert "proj/backend/app/main.py" in executor.files      # written where the gate looks
    assert "backend/app/main.py" not in executor.files        # NOT at the bare path
    # And the completeness gate is now satisfied for that target file.
    assert executor.files_complete("proj", ["backend/app/main.py"]).passed
    # The written file is recorded so a subsequent repair/read sees it.
    assert "proj/backend/app/main.py" in executor.files


def test_repair_does_not_double_prefix_an_already_prefixed_path() -> None:
    executor = FakeExecutor()
    # A model that returns the already-prefixed path (as shown in "current files") must not be
    # re-prefixed into proj/proj/....
    RepairAgent(executor=executor, llm=_FixedReplyLLM("proj/backend/app/main.py")).execute(_state())

    assert "proj/backend/app/main.py" in executor.files
    assert "proj/proj/backend/app/main.py" not in executor.files
