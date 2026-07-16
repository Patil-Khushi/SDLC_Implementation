"""Integration test: real MCPExecutor against a running exec-sandbox (Prompt 4 acceptance).

Bring the sandbox up first:
    docker compose up -d exec-sandbox egress-proxy
    SANDBOX_MCP_URL=http://localhost:8080/mcp pytest app/tests/test_mcp_integration.py

Marked @integration; when the sandbox is unreachable the test SKIPS cleanly, so the fast suite
(`pytest -m "not integration"`, or a plain run with no sandbox) stays green.
"""

import asyncio

import pytest

pytestmark = pytest.mark.integration


def _connect():
    from app.config.settings import get_settings
    from app.integrations.executor import MCPExecutor

    settings = get_settings()
    return asyncio.run(MCPExecutor.connect(settings.sandbox_mcp_url, settings.sandbox_mcp_transport))


def test_run_command_and_compile_against_sandbox() -> None:
    try:
        executor = _connect()
    except Exception as exc:  # sandbox not running / not reachable
        pytest.skip(f"exec-sandbox not reachable: {exc}")

    # 1. run_command echo hi -> exit_code 0
    result = executor.run_command(["echo", "hi"], cwd=".")
    assert result.exit_code == 0, result.stderr
    assert "hi" in result.stdout

    # 2. compile a trivially valid python file in the sandbox
    executor.write_file("proj/main.py", "print('ok')\n")
    check = executor.compile("proj")
    assert check.passed, check.stderr

    # 3. repair tools never expose git_commit (rule 2)
    names = {getattr(t, "name", None) for t in executor.get_repair_tools()}
    assert "git_commit" not in names
