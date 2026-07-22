"""exec-sandbox MCP server.

Exposes the tool surface ``app/integrations/executor.py::MCPExecutor`` calls over MCP
(streamable-http): ``run_command``, ``write_file``, ``read_file``, ``git_status``, ``git_diff``,
``install_package``, ``git_commit``. Runs INSIDE the container; the client (MCPExecutor) never
shells out itself — this process is the only thing that touches the filesystem/subprocess here.

All paths are relative to ``WORKSPACE_ROOT`` (default ``/workspace``) and are resolved+contained
the same way ``scripts/local_executor.py::LocalDiskExecutor._resolve`` does on the host side, so a
generated file's ``../../etc/passwd``-style path can never escape the workspace.

``run_command`` prepends ``<cwd>/.py_packages`` to ``PYTHONPATH`` — the fixed-path ``build()``
installs backend deps with ``pip install --target .py_packages`` (see ``executor.py``), and without
this, a subsequent ``python -m pytest``/``compileall`` in the same ``cwd`` would never see them.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace")).resolve()
WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("exec-sandbox", host="0.0.0.0", port=8080)


def _resolve(rel: str) -> Path:
    """Resolve ``rel`` under the workspace root; refuse a path that escapes it."""
    target = (WORKSPACE_ROOT / str(rel)).resolve()
    if target != WORKSPACE_ROOT and WORKSPACE_ROOT not in target.parents:
        raise ValueError(f"path escapes the sandbox workspace: {rel!r}")
    return target


def _child_env(cwd: Path) -> dict[str, str]:
    """The child process env: real environ + cwd-scoped PYTHONPATH/NODE_PATH for installed deps."""
    env = dict(os.environ)
    py_packages = str(cwd / ".py_packages")
    node_modules = str(cwd / "node_modules")
    env["PYTHONPATH"] = os.pathsep.join([py_packages, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env["NODE_PATH"] = os.pathsep.join([node_modules, env.get("NODE_PATH", "")]).rstrip(os.pathsep)
    return env


@mcp.tool()
def run_command(cmd: list[str], cwd: str = ".", timeout: float | None = None) -> dict[str, Any]:
    """Run ``cmd`` (argv list) inside the sandbox workspace. Returns stdout/stderr/exit_code/timed_out."""
    workdir = _resolve(cwd)
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout or 120,
            env=_child_env(workdir),
        )
        return {"stdout": proc.stdout, "stderr": proc.stderr, "exit_code": proc.returncode, "timed_out": False}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "[timed out]", "exit_code": 124, "timed_out": True}
    except FileNotFoundError as exc:
        return {"stdout": "", "stderr": f"command not found: {cmd[0]!r} ({exc})", "exit_code": 127, "timed_out": False}


@mcp.tool()
def write_file(path: str, content: str) -> dict[str, Any]:
    """Write ``content`` to ``path`` (workspace-relative), creating parent directories."""
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"ok": True}


@mcp.tool()
def read_file(path: str) -> str:
    """Return the text content of ``path`` (workspace-relative)."""
    return _resolve(path).read_text(encoding="utf-8")


@mcp.tool()
def git_status(project_dir: str) -> str:
    """Read-only ``git status --porcelain`` output for ``project_dir``."""
    r = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(_resolve(project_dir)), capture_output=True, text=True
    )
    return r.stdout + r.stderr


@mcp.tool()
def git_diff(project_dir: str) -> str:
    """Read-only ``git diff`` output for ``project_dir``."""
    r = subprocess.run(["git", "diff"], cwd=str(_resolve(project_dir)), capture_output=True, text=True)
    return r.stdout + r.stderr


@mcp.tool()
def install_package(name: str, manager: str = "pip", cwd: str = ".") -> dict[str, Any]:
    """Install ``name`` from a package registry. ``manager`` is ``"pip"`` (default) or ``"npm"``."""
    workdir = _resolve(cwd)
    workdir.mkdir(parents=True, exist_ok=True)
    if manager == "npm":
        cmd = ["npm", "install", "--no-audit", "--no-fund", name]
    else:
        cmd = ["python", "-m", "pip", "install", "--no-input", "--target", ".py_packages", name]
    try:
        proc = subprocess.run(cmd, cwd=str(workdir), capture_output=True, text=True, timeout=300, env=_child_env(workdir))
        return {"stdout": proc.stdout, "stderr": proc.stderr, "exit_code": proc.returncode, "timed_out": False}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "[timed out]", "exit_code": 124, "timed_out": True}


@mcp.tool()
def git_commit(project_dir: str, message: str) -> dict[str, Any]:
    """Fixed-path commit: ``git init`` (if absent) + ``add -A`` + ``commit -m``. FIXED PATH ONLY —
    this tool is never exposed to the LLM (see ``Executor.get_repair_tools`` on the client side)."""
    root = _resolve(project_dir)
    root.mkdir(parents=True, exist_ok=True)

    def _git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)

    if not (root / ".git").is_dir():
        _git("init")
    add = _git("add", "-A")
    if add.returncode != 0:
        return {"committed": False, "sha": None, "stdout": add.stdout, "stderr": add.stderr, "exit_code": add.returncode}
    commit = _git("commit", "-m", message)
    sha = None
    if commit.returncode == 0:
        sha = _git("rev-parse", "HEAD").stdout.strip() or None
    return {
        "committed": commit.returncode == 0,
        "sha": sha,
        "stdout": commit.stdout,
        "stderr": commit.stderr,
        "exit_code": commit.returncode,
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
