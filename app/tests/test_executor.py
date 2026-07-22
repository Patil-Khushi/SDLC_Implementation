"""Acceptance tests for the execution chokepoint (app/integrations/executor.py)."""

import pytest

from app.integrations.executor import (
    CheckResult,
    Executor,
    FakeExecutor,
    MCPExecutor,
    RunResult,
)


def test_fake_compile_fails_once_then_passes() -> None:
    """Headline acceptance: script "fail then pass" and observe the gate's view."""
    executor = FakeExecutor(compile_results=[False, True])

    first = executor.compile("proj")
    assert first.passed is False
    assert first.name == "compile"
    assert first.stderr != ""          # the gate/router reads this on failure
    assert first.exit_code != 0

    second = executor.compile("proj")
    assert second.passed is True
    assert second.stderr == ""


def test_gate_repair_loop_converges() -> None:
    """Simulate fixed→gate→repair→gate until compile passes (local cap 3)."""
    executor = FakeExecutor(compile_results=[False, True])
    repair_attempt = 0
    result = executor.compile("proj")
    while not result.passed and repair_attempt < 3:
        repair_attempt += 1
        result = executor.compile("proj")
    assert result.passed is True
    assert repair_attempt == 1         # failed once, passed on the retry


def test_default_pass_when_queue_exhausted() -> None:
    executor = FakeExecutor(compile_results=[False])
    assert executor.compile("p").passed is False
    assert executor.compile("p").passed is True


def test_all_four_checks_scriptable() -> None:
    executor = FakeExecutor(
        compile_results=[True], build_results=[False], test_results=[True], lint_results=[False]
    )
    assert executor.compile("p").passed is True
    assert executor.build("p").passed is False
    assert executor.test("p").passed is True
    assert executor.lint("p").passed is False


def test_scripted_checkresult_returned_verbatim() -> None:
    custom = CheckResult(name="test", passed=False, stderr="3 failed", exit_code=1)
    assert FakeExecutor(test_results=[custom]).test("p") is custom


def test_git_commit_is_fixed_path_and_records() -> None:
    executor = FakeExecutor()
    commit = executor.git_commit("proj", "feat: login endpoint")
    assert commit.committed is True
    assert commit.sha is not None
    assert executor.commits == [("proj", "feat: login endpoint")]


def test_repair_tools_exclude_git_commit() -> None:
    names = {t.name for t in FakeExecutor().get_repair_tools()}
    assert names == {"install_package", "run_command", "read_file", "git_status", "git_diff"}
    assert "git_commit" not in names   # CLAUDE.md rule 2: the LLM can never commit


def test_repair_run_command_refuses_git_writes() -> None:
    tools = {t.name: t for t in FakeExecutor().get_repair_tools()}
    with pytest.raises(PermissionError):
        tools["run_command"].handler(["git", "commit", "-m", "sneaky"], cwd="proj")
    with pytest.raises(PermissionError):
        tools["run_command"].handler(["git", "push"], cwd="proj")


def test_repair_run_command_allows_read_only_and_builds() -> None:
    executor = FakeExecutor(run_result=RunResult(stdout="ok", stderr="", exit_code=0))
    tools = {t.name: t for t in executor.get_repair_tools()}
    assert tools["run_command"].handler(["git", "status"], cwd="proj").ok is True
    tools["run_command"].handler(["npm", "run", "build"], cwd="proj")
    assert ["git", "status"] in executor.commands
    assert ["npm", "run", "build"] in executor.commands


def test_repair_tools_read_and_inspect() -> None:
    executor = FakeExecutor(files={"a.py": "print(1)"}, status_text="clean", diff_text="+1 -0")
    tools = {t.name: t for t in executor.get_repair_tools()}
    assert tools["read_file"].handler("a.py") == "print(1)"
    assert tools["git_status"].handler("proj") == "clean"
    assert tools["git_diff"].handler("proj") == "+1 -0"
    assert tools["install_package"].handler("proj", "requests").ok is True
    assert executor.installs == [("proj", "requests", "pip")]


def test_repair_tools_have_input_schemas() -> None:
    for tool in FakeExecutor().get_repair_tools():
        assert tool.input_schema.get("type") == "object"


def test_files_complete_passes_when_all_targets_written() -> None:
    executor = FakeExecutor(files={"p1/a.py": "x", "p1/b.py": "y"})
    result = executor.files_complete("p1", ["a.py", "b.py"])
    assert result.passed is True
    assert result.name == "files_complete"
    assert result.stderr == ""


def test_files_complete_fails_and_lists_every_missing_file() -> None:
    executor = FakeExecutor(files={"p1/a.py": "x"})
    result = executor.files_complete("p1", ["a.py", "b.py", "c.py"])
    assert result.passed is False
    assert "b.py" in result.stderr and "c.py" in result.stderr
    assert "a.py" not in result.stderr  # only the missing ones are reported


def test_write_read_roundtrip_and_missing() -> None:
    executor = FakeExecutor()
    executor.write_file("app/api/login.py", "content")
    assert executor.read_file("app/api/login.py") == "content"
    assert executor.writes == ["app/api/login.py"]
    with pytest.raises(FileNotFoundError):
        executor.read_file("nope.py")


def test_checkresult_from_run() -> None:
    assert CheckResult.from_run("compile", RunResult("", "", 0)).passed is True
    failing = CheckResult.from_run("compile", RunResult("", "boom", 1))
    assert failing.passed is False and failing.stderr == "boom"
    assert CheckResult.from_run("test", RunResult("", "", 0, timed_out=True)).passed is False


def test_fake_is_an_executor() -> None:
    assert isinstance(FakeExecutor(), Executor)


def test_mcp_executor_constructs_and_excludes_git_commit() -> None:
    # Real MCPExecutor (no live server): constructs from a tools list and still excludes
    # git_commit from the repair set. Live behavior is covered by test_mcp_integration.py.
    executor = MCPExecutor(client=None, tools=[])
    assert isinstance(executor, Executor)
    assert "git_commit" not in {getattr(t, "name", None) for t in executor.get_repair_tools()}
