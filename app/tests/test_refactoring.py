"""Refactoring agent: apply the fixes the code review named, written where the next agent looks.

The agent reads the review's structured findings (``review_findings_path`` JSON), skips suppressed
false positives, then runs an AGENTIC edit loop: the model is given ``read_file`` / ``write_file``
tools scoped to ``<project_dir>/`` and edits the flagged files directly (like a coding agent),
landing the fixes under the SAME prefix the code generator / repair path use — so the downstream
Debugging agent reads them where it expects. It records what it edited (``refactored_files``) and
persists a Markdown report, but never commits or runs a gate itself — the fixed
``refactoring_publish`` node does the git work (see test_refactoring_publish.py). A
missing/unreadable findings file is surfaced as ``needs_human_review``, not silently treated as
"nothing to do".
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.agents.refactoring import MAX_FILES_PER_RUN, RefactoringAgent
from app.config.settings import get_settings
from app.integrations.executor import FakeExecutor


class _StubLLM:
    """Agentic gateway stand-in: parses the flagged files from the prompt and APPLIES a fix to each
    by driving the ``write_file`` tool (reading first), mimicking how the real model edits files in
    the tool loop. Only writes files it can read, so a not-found file is left untouched — as the
    real model would. Pass ``path=`` to force the exact path written (the double-prefix case).
    """

    def __init__(self, content: str = "print(1)\n", path: str | None = None) -> None:
        self._content = content
        self._path = path
        self.prompts: list[str] = []

    def complete_with_tools(self, prompt: str, *, system: str | None = None,
                            tools: list | None = None, max_iters: int = 4) -> str:
        self.prompts.append(prompt)
        by_name = {t.name: t for t in (tools or [])}
        read, write = by_name.get("read_file"), by_name.get("write_file")
        targets = [self._path] if self._path is not None else re.findall(r"^File: (.+)$", prompt, re.MULTILINE)
        for f in targets:
            if read is not None and str(read.handler(path=f)).startswith("ERROR"):
                continue  # couldn't read it -> don't write (mirrors the real model)
            if write is not None:
                write.handler(path=f, content=self._content)
        return "done"


def _findings_file(tmp_path: Path, findings: list[dict[str, Any]]) -> str:
    p = tmp_path / "findings.json"
    p.write_text(json.dumps(findings), encoding="utf-8")
    return str(p)


def _reports_findings(subfolder: str, findings: list[dict[str, Any]], report_md: str | None = None) -> Path:
    """Write findings.json (and optionally report.md) into <reports_dir>/<subfolder>/, the layout
    CodeReviewAgent._finish produces — so the agent can locate them by SCANNING the reports dir.
    reports_dir is the autouse-fixture tmp path (conftest._reports_to_tmp)."""
    run_dir = Path(get_settings().reports_dir) / subfolder
    run_dir.mkdir(parents=True, exist_ok=True)
    fj = run_dir / "findings.json"
    fj.write_text(json.dumps(findings), encoding="utf-8")
    if report_md is not None:
        (run_dir / "report.md").write_text(report_md, encoding="utf-8")
    return fj


def _open(file: str, **over: Any) -> dict[str, Any]:
    f: dict[str, Any] = {"file": file, "line": 1, "severity": "High",
                         "category": "Bug", "rule_id": "B001", "message": "wrong literal",
                         "status": "Open"}
    f.update(over)
    return f


def _state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"run_id": "r1", "project_id": "proj", "generated_code": []}
    base.update(over)
    return base


def test_applies_fix_from_findings_json(tmp_path: Path) -> None:
    executor = FakeExecutor(files={"proj/src/foo.py": "print(0)\n"})
    state = _state(review_findings_path=_findings_file(tmp_path, [_open("src/foo.py")]))

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(state)

    assert executor.files["proj/src/foo.py"] == "print(1)\n"      # written where the next agent looks
    assert "proj/src/foo.py" in state["generated_code"]           # recorded for the downstream read
    assert state["refactored_files"] == ["proj/src/foo.py"]       # the publish node commits these
    assert state["workflow_status"] == "refactored"


def test_prompt_carries_the_findings(tmp_path: Path) -> None:
    executor = FakeExecutor(files={"proj/src/foo.py": "print(0)\n"})
    findings = [_open("src/foo.py", line=7, message="unique-finding-message")]
    llm = _StubLLM()
    RefactoringAgent(executor=executor, llm=llm).execute(
        _state(review_findings_path=_findings_file(tmp_path, findings))
    )

    assert llm.prompts and "unique-finding-message" in llm.prompts[0]
    assert "src/foo.py" in llm.prompts[0]


def test_does_not_double_prefix_an_already_prefixed_path(tmp_path: Path) -> None:
    executor = FakeExecutor(files={"proj/src/foo.py": "print(0)\n"})
    # A model that echoes the already-prefixed path must not be re-prefixed into proj/proj/....
    RefactoringAgent(executor=executor, llm=_StubLLM(path="proj/src/foo.py")).execute(
        _state(review_findings_path=_findings_file(tmp_path, [_open("src/foo.py")]))
    )

    assert "proj/src/foo.py" in executor.files
    assert "proj/proj/src/foo.py" not in executor.files


def test_skips_suppressed_findings(tmp_path: Path) -> None:
    # An Open finding on foo.py (fix it) alongside a Suppressed false positive on a test file
    # (leave it — fixing it would undo Code Review's suppression and can break the test).
    executor = FakeExecutor(files={
        "proj/src/foo.py": "print(0)\n",
        "proj/tests/test_thing.py": "assert compute() == 3\n",
    })
    findings = [
        _open("src/foo.py"),
        _open("tests/test_thing.py", rule_id="S101", category="Security",
              message="assert used", status="Suppressed"),
    ]
    state = _state(review_findings_path=_findings_file(tmp_path, findings))

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(state)

    assert executor.files["proj/src/foo.py"] == "print(1)\n"                 # Open finding fixed
    assert executor.files["proj/tests/test_thing.py"] == "assert compute() == 3\n"  # suppressed untouched
    assert executor.writes == ["proj/src/foo.py"]                            # only the Open file written


def test_all_suppressed_is_a_clean_noop(tmp_path: Path) -> None:
    executor = FakeExecutor(files={"proj/tests/test_thing.py": "assert x\n"})
    findings = [_open("tests/test_thing.py", rule_id="S101", status="Suppressed")]
    state = _state(review_findings_path=_findings_file(tmp_path, findings))

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(state)

    assert executor.writes == []
    assert state["workflow_status"] == "refactored"
    assert "No actionable" in state["refactored_code"]


def test_skips_file_not_in_workspace(tmp_path: Path) -> None:
    executor = FakeExecutor()  # empty workspace
    state = _state(review_findings_path=_findings_file(tmp_path, [_open("src/missing.py")]))

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(state)

    assert executor.writes == []                                   # wrote nothing
    assert state["workflow_status"] == "refactored"
    assert "not found" in state["refactored_code"]


def test_missing_findings_path_surfaces_failure() -> None:
    executor = FakeExecutor(files={"proj/src/foo.py": "print(0)\n"})
    state = _state()  # no review_findings_path at all

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(state)

    assert executor.writes == []
    assert state["workflow_status"] == "needs_human_review"        # surfaced, not a silent no-op
    assert "unavailable" in state["refactored_code"]


def test_unreadable_findings_path_surfaces_failure(tmp_path: Path) -> None:
    executor = FakeExecutor(files={"proj/src/foo.py": "print(0)\n"})
    state = _state(review_findings_path=str(tmp_path / "does-not-exist.json"))

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(state)

    assert executor.writes == []
    assert state["workflow_status"] == "needs_human_review"
    assert "could not read" in state["refactored_code"]


def test_bad_llm_reply_writes_nothing(tmp_path: Path) -> None:
    executor = FakeExecutor(files={"proj/src/foo.py": "print(0)\n"})

    class _JunkLLM:
        def complete_with_tools(self, prompt: str, *, system: str | None = None,
                                tools: list | None = None, max_iters: int = 4) -> str:
            return "sorry, I can't help with that"

    state = _state(review_findings_path=_findings_file(tmp_path, [_open("src/foo.py")]))
    RefactoringAgent(executor=executor, llm=_JunkLLM()).execute(state)

    assert executor.files["proj/src/foo.py"] == "print(0)\n"       # unchanged, no partial garbage
    assert executor.writes == []
    assert state["workflow_status"] == "refactored"


def test_writes_a_refactoring_report(tmp_path: Path) -> None:
    # Every run persists a Markdown report next to the Code Review report
    # (reports/<project>-<run>/refactoring-report.md) and records its path + content in state.
    executor = FakeExecutor(files={"proj/src/foo.py": "print(0)\n"})
    state = _state(review_findings_path=_findings_file(tmp_path, [_open("src/foo.py")]))

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(state)

    report_path = Path(state["refactoring_report_path"])
    assert report_path.name == "refactoring-report.md"
    assert report_path.parent.name == "proj-r1"                    # same run folder as code review
    report = report_path.read_text(encoding="utf-8")
    assert report == state["refactoring_report"]
    assert "# Refactoring Report" in report
    assert "src/foo.py" in report                                  # the edited file is listed
    assert "| Run ID | r1 |" in report


def test_report_written_even_when_findings_unavailable() -> None:
    # The early-exit paths still leave a report explaining WHY nothing was refactored.
    executor = FakeExecutor(files={"proj/src/foo.py": "print(0)\n"})
    state = _state()  # no review_findings_path

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(state)

    assert state["refactored_files"] == []                         # publish step will be a no-op
    report = Path(state["refactoring_report_path"]).read_text(encoding="utf-8")
    assert "findings unavailable" in report
    assert "(no files were edited)" in report


def test_report_folder_matches_code_review_when_project_id_is_empty(tmp_path: Path) -> None:
    # CodeReviewAgent falls back to run_id when project_id is falsy (code_review.py's
    # `project_id or run_id or "project"`, applied BEFORE slugging) — the refactoring report must
    # land in the SAME run folder, not a different "run-<id>" folder from a bare `_slug("")`.
    executor = FakeExecutor(files={"abc123/src/foo.py": "print(0)\n"})
    state = _state(project_id="", run_id="abc123",
                    review_findings_path=_findings_file(tmp_path, [_open("src/foo.py")]))

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(state)

    report_path = Path(state["refactoring_report_path"])
    assert report_path.parent.name == "abc123-abc123"       # matches CodeReviewAgent._finish's folder


def test_finds_findings_by_scanning_reports_dir() -> None:
    # No review_findings_path on state: the agent locates findings.json by scanning the reports
    # dir for the newest <subfolder>/findings.json (where Code Review writes it).
    _reports_findings("proj-r1", [_open("src/foo.py")])
    executor = FakeExecutor(files={"proj/src/foo.py": "print(0)\n"})
    state = _state()  # deliberately no review_findings_path

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(state)

    assert executor.files["proj/src/foo.py"] == "print(1)\n"        # located + fixed via the scan
    assert state["refactored_files"] == ["proj/src/foo.py"]
    assert state["workflow_status"] == "refactored"


def test_scan_picks_the_newest_findings_folder() -> None:
    # Two report folders: the more recently modified one wins (later mtime).
    _reports_findings("old-run", [_open("src/old.py")])
    newest = _reports_findings("new-run", [_open("src/new.py")])
    import os
    import time

    os.utime(newest, (time.time() + 10, time.time() + 10))  # force new-run to be newest
    executor = FakeExecutor(files={"proj/src/old.py": "print(0)\n", "proj/src/new.py": "print(0)\n"})

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(_state())

    assert executor.files["proj/src/new.py"] == "print(1)\n"        # the newest folder's finding
    assert executor.files["proj/src/old.py"] == "print(0)\n"        # the older folder ignored


def test_report_md_is_added_to_prompt_as_context() -> None:
    # report.md sitting next to findings.json is passed to the model as extra context.
    _reports_findings("proj-r1", [_open("src/foo.py")], report_md="UNIQUE-REPORT-MARKER recommendation")
    executor = FakeExecutor(files={"proj/src/foo.py": "print(0)\n"})
    llm = _StubLLM()

    RefactoringAgent(executor=executor, llm=llm).execute(_state())

    assert llm.prompts and "UNIQUE-REPORT-MARKER" in llm.prompts[0]  # report prose reached the model
    assert "src/foo.py" in llm.prompts[0]                            # findings still present


def test_state_path_used_when_reports_dir_has_no_findings(tmp_path: Path) -> None:
    # No scan hit (empty reports dir) -> use the exact path Code Review recorded on state.
    executor = FakeExecutor(files={"proj/src/foo.py": "print(0)\n"})
    state = _state(review_findings_path=_findings_file(tmp_path, [_open("src/foo.py")]))

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(state)

    assert executor.files["proj/src/foo.py"] == "print(1)\n"        # located via the state path


def test_explicit_state_path_wins_over_reports_scan(tmp_path: Path) -> None:
    # A caller that prepared its own findings (e.g. run_refactoring.py's normalized file) must not
    # be overridden by a stale findings.json the scan finds in the reports dir. State path wins.
    _reports_findings("stale-run", [_open("src/stale.py")])          # would be picked by a scan
    executor = FakeExecutor(files={"proj/src/chosen.py": "print(0)\n", "proj/src/stale.py": "print(0)\n"})
    state = _state(review_findings_path=_findings_file(tmp_path, [_open("src/chosen.py")]))

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(state)

    assert executor.files["proj/src/chosen.py"] == "print(1)\n"      # the explicit state file won
    assert executor.files["proj/src/stale.py"] == "print(0)\n"       # the reports scan was ignored


def test_defers_files_over_the_fan_out_cap(tmp_path: Path) -> None:
    # More findings-bearing files than the cap: the first MAX_FILES_PER_RUN are fixed, the rest are
    # REPORTED as deferred (not silently dropped or processed). Zero-padded names keep sort order.
    n = MAX_FILES_PER_RUN + 5
    files = {f"proj/src/f{i:02d}.py": "print(0)\n" for i in range(n)}
    findings = [_open(f"src/f{i:02d}.py") for i in range(n)]
    executor = FakeExecutor(files=dict(files))
    state = _state(review_findings_path=_findings_file(tmp_path, findings))

    RefactoringAgent(executor=executor, llm=_StubLLM()).execute(state)

    assert len(executor.writes) == MAX_FILES_PER_RUN               # exactly the cap were fixed
    assert "proj/src/f00.py" in executor.writes                    # an early file was fixed
    assert "proj/src/f29.py" not in executor.writes               # an over-cap file was NOT fixed
    assert "Deferred" in state["refactored_code"]                  # and it's reported, not dropped
    assert state["workflow_status"] == "refactored"
