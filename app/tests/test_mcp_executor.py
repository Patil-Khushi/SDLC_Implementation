"""Fast unit tests for MCPExecutor — no Docker, no live server.

Fake MCP tools exercise the sync→async bridge, result coercion, and repair-tool filtering.
The live run_command/compile path is covered by test_mcp_integration.py (@integration).
"""

import json

from app.integrations.executor import MCPExecutor, RunResult


class _FakeTool:
    """Minimal stand-in for a LangChain MCP tool: has a name and an async ainvoke."""

    def __init__(self, name: str, result: object = None) -> None:
        self.name = name
        self._result = result
        self.calls: list[dict] = []

    async def ainvoke(self, args: dict) -> object:
        self.calls.append(args)
        return self._result


def test_run_command_parses_dict_result() -> None:
    tool = _FakeTool("run_command", {"stdout": "hi", "stderr": "", "exit_code": 0, "timed_out": False})
    executor = MCPExecutor(client=None, tools=[tool])
    result = executor.run_command(["echo", "hi"])
    assert isinstance(result, RunResult)
    assert result.exit_code == 0
    assert result.stdout == "hi"
    assert result.ok is True


def test_run_command_parses_json_string_result() -> None:
    payload = json.dumps({"stdout": "", "stderr": "boom", "exit_code": 1, "timed_out": False})
    executor = MCPExecutor(client=None, tools=[_FakeTool("run_command", payload)])
    result = executor.run_command(["false"])
    assert result.exit_code == 1
    assert result.stderr == "boom"
    assert result.ok is False


def test_read_file_coerces_text() -> None:
    executor = MCPExecutor(client=None, tools=[_FakeTool("read_file", "file-content")])
    assert executor.read_file("a.py") == "file-content"


def test_repair_tools_exclude_git_commit_and_write_file() -> None:
    names = ["run_command", "write_file", "read_file", "git_status", "git_diff", "git_commit", "install_package"]
    executor = MCPExecutor(client=None, tools=[_FakeTool(n) for n in names])
    repair_names = {getattr(t, "name", None) for t in executor.get_repair_tools()}
    assert repair_names == {"install_package", "read_file", "git_status", "git_diff", "run_command"}
    assert "git_commit" not in repair_names   # rule 2: LLM can never commit
    assert "write_file" not in repair_names   # repair proposes content; fixed code writes it


def test_install_package_passes_through_the_requested_manager() -> None:
    # Regression: install_package used to hardcode manager="pip" regardless of the caller's
    # request, silently breaking npm installs for a MERN/Node work item's repair path.
    tool = _FakeTool("install_package", {"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False})
    executor = MCPExecutor(client=None, tools=[tool])

    executor.install_package("proj", "requests")
    assert tool.calls[-1]["manager"] == "pip"  # default unchanged

    executor.install_package("proj", "left-pad", manager="npm")
    assert tool.calls[-1] == {"name": "left-pad", "manager": "npm", "cwd": "proj"}


def test_repair_run_command_is_scoped_against_git_writes() -> None:
    executor = MCPExecutor(client=None, tools=[_FakeTool("run_command", {"stdout": "", "stderr": "", "exit_code": 0})])
    import pytest

    with pytest.raises(PermissionError):
        executor._repair_run_command(["git", "commit", "-m", "sneaky"])
    # a read-only git command is allowed through (delegates to the underlying tool)
    assert executor._repair_run_command(["git", "status"])["exit_code"] == 0


# --- present-but-null payload fields must never become the literal string "None" ----------------

def test_run_command_coalesces_null_stdout_stderr_not_stringifies_them() -> None:
    # A sandbox payload with the key PRESENT but JSON null (e.g. a command that wrote nothing to
    # stderr) must not turn into the 4-char string "None" — that corrupted string would otherwise
    # be persisted into WorkflowState and fed to the Debugging agent as if it were real output.
    tool = _FakeTool("run_command", {"stdout": None, "stderr": None, "exit_code": 0, "timed_out": False})
    result = MCPExecutor(client=None, tools=[tool]).run_command(["true"])
    assert result.stdout == ""
    assert result.stderr == ""
    assert "None" not in result.stdout and "None" not in result.stderr


def test_run_command_null_exit_code_falls_back_not_crashes() -> None:
    # exit_code needs an explicit None-check, not `d.get(...) or -1`: 0 is falsy in Python, so a
    # legitimate successful run must not be misread as -1 — but a genuinely absent/null exit_code
    # (malformed sandbox payload) must fall back to -1 rather than `int(None)` raising.
    tool = _FakeTool("run_command", {"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False})
    assert MCPExecutor(client=None, tools=[tool]).run_command(["true"]).exit_code == 0

    tool_null = _FakeTool("run_command", {"stdout": "", "stderr": "", "exit_code": None, "timed_out": False})
    assert MCPExecutor(client=None, tools=[tool_null]).run_command(["true"]).exit_code == -1


def test_install_package_coalesces_null_fields() -> None:
    tool = _FakeTool("install_package", {"stdout": None, "stderr": None, "exit_code": None, "timed_out": False})
    result = MCPExecutor(client=None, tools=[tool]).install_package("proj", "left-pad", manager="npm")
    assert result.stdout == "" and result.stderr == ""
    assert result.exit_code == -1


def test_git_commit_coalesces_null_stdout_stderr_and_exit_code() -> None:
    # git_commit predates the run_command/install_package null-handling fix and was missed the
    # first time round. Worse than the "None"-string case elsewhere: `int(d.get("exit_code", -1))`
    # on a PRESENT-but-null exit_code is `int(None)`, which raises — and git_commit is a FIXED,
    # non-retryable step, so that crash takes down the whole run rather than just one repair round.
    tool = _FakeTool(
        "git_commit",
        {"committed": True, "sha": None, "stdout": None, "stderr": None, "exit_code": None},
    )
    result = MCPExecutor(client=None, tools=[tool]).git_commit("proj", "a commit message")
    assert result.committed is True
    assert result.sha is None            # a null sha is a legitimate value, not corruption
    assert result.stdout == "" and "None" not in result.stdout
    assert result.stderr == "" and "None" not in result.stderr
    assert result.exit_code == -1        # falls back cleanly instead of raising


def test_git_commit_zero_exit_code_is_not_misread_as_failure() -> None:
    tool = _FakeTool(
        "git_commit",
        {"committed": True, "sha": "abc123", "stdout": "", "stderr": "", "exit_code": 0},
    )
    result = MCPExecutor(client=None, tools=[tool]).git_commit("proj", "msg")
    assert result.exit_code == 0
    assert result.sha == "abc123"


# --- output capping must protect the sandbox path too, not just the local-disk one ---------------

def test_run_command_caps_oversized_output() -> None:
    # Real incident this guards against: a project-wide `npm test` produced 7.5M chars of stderr,
    # which then blew straight past the LLM's context limit and wasted an entire repair round. The
    # cap originally shipped only in scripts/local_executor.py, leaving MCPExecutor (the actual
    # sandbox path used in production) exposed to the same failure mode.
    huge = "x" * 50_000
    tool = _FakeTool("run_command", {"stdout": huge, "stderr": huge, "exit_code": 1, "timed_out": False})
    result = MCPExecutor(client=None, tools=[tool]).run_command(["npm", "test"])
    assert len(result.stdout) < 50_000
    assert len(result.stderr) < 50_000
    assert "truncated" in result.stdout and "truncated" in result.stderr


def test_install_package_caps_oversized_output() -> None:
    huge = "y" * 50_000
    tool = _FakeTool("install_package", {"stdout": huge, "stderr": huge, "exit_code": 1, "timed_out": False})
    result = MCPExecutor(client=None, tools=[tool]).install_package("proj", "some-pkg", manager="npm")
    assert len(result.stdout) < 50_000
    assert "truncated" in result.stdout
