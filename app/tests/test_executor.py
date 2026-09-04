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


# --- list_files: the one tree primitive on the interface ---------------------------------------
# Before this, enumerating a tree meant an ad-hoc run_command(["git","ls-files"]) that every caller
# re-rolled and that FakeExecutor could not answer (it returns one canned RunResult) — so anything
# needing a file list was untestable through the normal fake.


def test_list_files_enumerates_the_project_relative_tree() -> None:
    ex = FakeExecutor(files={
        "proj/src/a.py": "", "proj/src/nested/b.py": "", "proj/README.md": "",
        "other/c.py": "",                      # a DIFFERENT project — must not leak in
    })
    assert ex.list_files("proj") == ["README.md", "src/a.py", "src/nested/b.py"]


def test_list_files_filters_by_prefix() -> None:
    ex = FakeExecutor(files={"proj/src/a.py": "", "proj/tests/t.py": "", "proj/README.md": ""})
    assert ex.list_files("proj", prefix="src/") == ["src/a.py"]


def test_list_files_skips_vcs_dependency_and_build_noise() -> None:
    # node_modules alone would swamp a listing with tens of thousands of entries.
    ex = FakeExecutor(files={
        "proj/src/a.py": "",
        "proj/node_modules/pkg/index.js": "",
        "proj/.git/config": "",
        "proj/dist/bundle.js": "",
        "proj/src/__pycache__/a.pyc": "",
    })
    assert ex.list_files("proj") == ["src/a.py"]


def test_list_files_is_empty_for_an_unknown_project() -> None:
    # "Nothing to list" is a legitimate answer for a caller surveying a tree — never an exception.
    assert FakeExecutor(files={"proj/a.py": ""}).list_files("does-not-exist") == []


def test_clean_listing_normalizes_both_backends_identically() -> None:
    # git ls-files emits bare paths; `find .` prefixes ./ — the two backends must agree on a tree.
    from app.integrations.executor import clean_listing

    git_out = "src/a.py\nREADME.md\nnode_modules/x/i.js\n"
    find_out = "./src/a.py\n./README.md\n./node_modules/x/i.js\n"
    assert clean_listing(git_out) == clean_listing(find_out) == ["README.md", "src/a.py"]


def test_list_files_is_on_the_interface() -> None:
    # It is abstract, so every executor must implement it — that is the point of putting it here
    # rather than leaving each caller its own git ls-files.
    assert "list_files" in Executor.__abstractmethods__
