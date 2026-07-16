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

    async def ainvoke(self, args: dict) -> object:
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


def test_repair_run_command_is_scoped_against_git_writes() -> None:
    executor = MCPExecutor(client=None, tools=[_FakeTool("run_command", {"stdout": "", "stderr": "", "exit_code": 0})])
    import pytest

    with pytest.raises(PermissionError):
        executor._repair_run_command(["git", "commit", "-m", "sneaky"])
    # a read-only git command is allowed through (delegates to the underlying tool)
    assert executor._repair_run_command(["git", "status"])["exit_code"] == 0
