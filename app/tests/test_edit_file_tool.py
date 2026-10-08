"""``edit_file``: an exact, single-snippet replacement in the shared editing toolset, opt-in.

It exists so a three-line fix doesn't have to carry a whole file back through the model's output
budget. Its contract: the snippet must match exactly once, writes stay inside ``allowed_paths``
when one is given, and agents that didn't ask for it (the brownfield Change Modifier) never see it.
"""

from __future__ import annotations

from app.agents.editing import editing_tools
from app.integrations.executor import FakeExecutor, RunResult, aggregate_check_results


def _tools(executor: FakeExecutor, touched: list[str], **kw):
    return {t.name: t.handler for t in editing_tools(executor, "proj", touched, edit=True, **kw)}


def test_edit_file_is_opt_in() -> None:
    names = [t.name for t in editing_tools(FakeExecutor(), "proj", [])]
    assert names == ["read_file", "write_file"]          # the Change Modifier's reviewed tool list
    assert "edit_file" in [t.name for t in editing_tools(FakeExecutor(), "proj", [], edit=True)]


def test_edit_file_replaces_exactly_one_occurrence() -> None:
    executor = FakeExecutor(files={"proj/src/a.ts": "const a = 1;\nconst b = 2;\n"})
    touched: list[str] = []
    result = _tools(executor, touched)["edit_file"](path="src/a.ts", old="const b = 2;", new="const b = 3;")
    assert result.startswith("edited src/a.ts")
    assert executor.files["proj/src/a.ts"] == "const a = 1;\nconst b = 3;\n"
    assert touched == ["proj/src/a.ts"]


def test_edit_file_refuses_missing_or_ambiguous_snippets_without_writing() -> None:
    executor = FakeExecutor(files={"proj/src/a.ts": "x();\nx();\n"})
    touched: list[str] = []
    edit = _tools(executor, touched)["edit_file"]
    assert "not found" in edit(path="src/a.ts", old="y();", new="z();")
    assert "occurs 2 times" in edit(path="src/a.ts", old="x();", new="z();")
    assert "is empty" in edit(path="src/a.ts", old="", new="z();")
    assert executor.files["proj/src/a.ts"] == "x();\nx();\n"
    assert touched == []


def test_edit_file_on_a_missing_file_points_at_write_file() -> None:
    result = _tools(FakeExecutor(), [])["edit_file"](path="src/new.ts", old="a", new="b")
    assert "write_file" in result


def test_edit_file_respects_allowed_paths() -> None:
    executor = FakeExecutor(files={"proj/src/a.ts": "a", "proj/src/b.ts": "b"})
    edit = _tools(executor, [], allowed_paths={"src/a.ts"})["edit_file"]
    assert edit(path="src/b.ts", old="b", new="B").startswith("REFUSED")
    assert executor.files["proj/src/b.ts"] == "b"
    assert edit(path="src/a.ts", old="a", new="A").startswith("edited")


def test_aggregate_reports_every_failing_tool_not_just_the_first() -> None:
    ok = RunResult("", "", 0)
    tsc = RunResult("src/a.ts(1,1): error TS1: x", "", 2)
    pip = RunResult("", "error: Microsoft Visual C++ 14.0 or greater is required.", 1, timed_out=True)
    result = aggregate_check_results(
        "build", [("py", ok, ""), ("tsc[proj/fe]", tsc, "proj/fe"), ("pip[proj/be]", pip, "proj/be")]
    )
    assert not result.passed
    assert "[tsc[proj/fe]] src/a.ts(1,1)" in result.stderr
    assert "[pip[proj/be]] error: Microsoft Visual C++" in result.stderr     # used to be dropped
    assert result.scope == "proj/fe" and result.exit_code == 2 and result.timed_out is True
    assert aggregate_check_results("build", [("py", ok, "")]).passed
