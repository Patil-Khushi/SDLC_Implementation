"""The single execution chokepoint for the Implementation Service.

Per DEVELOPER_GUIDE.md rule 6 (outside tools live in ``integrations/``) and CLAUDE.md:

* All command execution against generated code — compile / build / test / lint, dependency
  install, and git — goes through the ONE interface defined here (:class:`Executor`). No other
  module shells out or imports the MCP client.
* Both hybrid paths share this interface. The FIXED path (your node code) calls the check /
  commit methods directly. The REPAIR path gets only the subset returned by
  :meth:`Executor.get_repair_tools` — which deliberately EXCLUDES ``git_commit`` (rule 2) and
  whose ``run_command`` is scoped to refuse git writes.

Implementations:
* :class:`FakeExecutor` — scriptable, in-memory, for unit tests.
* :class:`MCPExecutor` — real; holds a ``MultiServerMCPClient`` pointed at the exec-sandbox and
  maps fixed-path checks to direct ``tool.ainvoke`` calls. The MCP client is imported lazily
  inside this file only, so nothing outside ``executor.py`` depends on it.

Agents/nodes obtain the active executor via :func:`get_executor` (set once in the FastAPI
lifespan via :func:`set_executor`); they never construct one.

Import scope (rules 5-6): execution methods are called only from graph nodes
(``app/graph/nodes.py``) and agents (``app/agents/``). The MCP client is constructed only here
and injected once in the app lifespan (``app/main.py``); no other module imports it. The
sandbox egress allowlist (PyPI + npm only) lives in ``tools/exec-sandbox/`` (``squid.conf`` +
the root ``docker-compose.yml``).
"""

from __future__ import annotations

import asyncio
import json
import os
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "StrPath",
    "RunResult",
    "CheckResult",
    "CommitResult",
    "RepairTool",
    "Executor",
    "FakeExecutor",
    "MCPExecutor",
    "get_executor",
    "set_executor",
]

StrPath = str | Path


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class RunResult:
    """Raw outcome of a single command run in the sandbox."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a fixed-path check (compile/build/test/lint).

    Carries pass/fail + captured stderr — this is what the gate/router reads to decide
    proceed / repair / escalate (CLAUDE.md rule 3). Maps 1:1 onto ``graph.state.GateCheck``.
    """

    name: str            # "compile" | "build" | "test" | "lint"
    passed: bool
    stderr: str = ""     # captured stderr; empty when passed
    stdout: str = ""
    exit_code: int = 0
    timed_out: bool = False

    @classmethod
    def from_run(cls, name: str, run: RunResult) -> CheckResult:
        """Build a CheckResult from a RunResult (passed == the command exited cleanly)."""
        return cls(
            name=name,
            passed=run.ok,
            stderr=run.stderr,
            stdout=run.stdout,
            exit_code=run.exit_code,
            timed_out=run.timed_out,
        )


@dataclass(frozen=True)
class CommitResult:
    """Outcome of a fixed-path git commit."""

    committed: bool
    sha: str | None = None
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


@dataclass(frozen=True)
class RepairTool:
    """A repair-path tool wrapper (used by FakeExecutor / the default implementation).

    ``handler`` is a bound executor method; ``input_schema`` is a JSON Schema for its args.
    (``MCPExecutor`` instead returns real LangChain tool objects — see its ``get_repair_tools``.)
    """

    name: str
    description: str
    handler: Callable[..., Any]
    input_schema: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- interface

# git subcommands that mutate history/worktree — forbidden on the repair path (rule 2).
_GIT_WRITE_SUBCOMMANDS = frozenset(
    {
        "commit", "add", "push", "reset", "rm", "mv", "checkout", "restore",
        "merge", "rebase", "cherry-pick", "revert", "apply", "stash", "tag",
        "branch", "init", "clean", "gc", "config",
    }
)


class Executor(ABC):
    """The one interface both hybrid paths use."""

    # -- shared primitives ---------------------------------------------------

    @abstractmethod
    def run_command(self, cmd: Sequence[str], cwd: StrPath = ".", timeout: float | None = None) -> RunResult:
        """Run ``cmd`` (argv list) in ``cwd`` inside the sandbox."""

    @abstractmethod
    def write_file(self, path: StrPath, content: str) -> None:
        """Write ``content`` to ``path`` in the workspace."""

    @abstractmethod
    def read_file(self, path: StrPath) -> str:
        """Return the text content of ``path``."""

    @abstractmethod
    def git_status(self, project_dir: StrPath) -> str:
        """Read-only ``git status`` output for ``project_dir``."""

    @abstractmethod
    def git_diff(self, project_dir: StrPath) -> str:
        """Read-only ``git diff`` output for ``project_dir``."""

    @abstractmethod
    def install_package(self, project_dir: StrPath, package: str, manager: str = "pip") -> RunResult:
        """Install a dependency from a package registry (workspace-scoped in the real sandbox).

        ``manager`` is ``"pip"`` (default) or ``"npm"`` — a MERN/Node work item's repair path
        needs npm installs too; defaulting to "pip" keeps every existing (2-arg) call site working.
        """

    # -- fixed-path checks ---------------------------------------------------

    @abstractmethod
    def files_complete(self, project_dir: StrPath, target_files: Sequence[str]) -> CheckResult:
        """Check every path in ``target_files`` exists under ``project_dir``. Fixed path only.

        Runs FIRST in the gate (before compile/build) so a partial generation reply never
        reaches the compiler — it's authoritative from disk, not a self-reported count, so it
        works identically whether the files came from code_generator or a repair rework pass.
        """

    @abstractmethod
    def compile(self, project_dir: StrPath) -> CheckResult:
        """Compile/type-check the project. Fixed path only."""

    @abstractmethod
    def build(self, project_dir: StrPath) -> CheckResult:
        """Build the project. Fixed path only."""

    @abstractmethod
    def test(self, project_dir: StrPath) -> CheckResult:
        """Run the project's tests. Fixed path only."""

    @abstractmethod
    def lint(self, project_dir: StrPath) -> CheckResult:
        """Lint the project. Fixed path only."""

    # -- fixed-path ONLY: never exposed to the LLM ---------------------------

    @abstractmethod
    def git_commit(self, project_dir: StrPath, message: str) -> CommitResult:
        """Commit staged work. FIXED PATH ONLY — never in :meth:`get_repair_tools` (rule 2)."""

    # -- repair-path exposure (concrete; identical for every implementation) --

    def _scoped_run_command(
        self, cmd: Sequence[str], cwd: StrPath = ".", timeout: float | None = None
    ) -> RunResult:
        """``run_command`` for the repair path, with git writes refused (rule 2)."""
        self._reject_forbidden(cmd)
        return self.run_command(cmd, cwd, timeout)

    @staticmethod
    def _reject_forbidden(cmd: Sequence[str]) -> None:
        argv = list(cmd)
        if not argv:
            raise ValueError("empty command")
        exe = os.path.basename(argv[0]).lower()
        if exe in {"git", "git.exe"}:
            sub = argv[1] if len(argv) > 1 else ""
            if sub in _GIT_WRITE_SUBCOMMANDS:
                raise PermissionError(
                    f"repair path may not run 'git {sub}': git writes are fixed-path only "
                    "(CLAUDE.md rule 2). Use git_status/git_diff to inspect."
                )

    def get_repair_tools(self) -> list[Any]:
        """The exact set of tools the LLM may call on the repair path.

        Default (FakeExecutor / tests): :class:`RepairTool` wrappers. ``MCPExecutor`` overrides
        this to return real LangChain tool objects. Either way ``git_commit`` is absent and
        ``run_command`` is the *scoped* variant.
        """
        path_arg = {"path": {"type": "string"}}
        project_arg = {"project_dir": {"type": "string"}}
        return [
            RepairTool(
                name="install_package",
                description="Install a dependency from a package registry (pip or npm).",
                handler=self.install_package,
                input_schema={
                    "type": "object",
                    "properties": {
                        **project_arg,
                        "package": {"type": "string"},
                        "manager": {"type": "string", "enum": ["pip", "npm"], "default": "pip"},
                    },
                    "required": ["project_dir", "package"],
                },
            ),
            RepairTool(
                name="run_command",
                description="Run a command in the sandbox. Scoped: git write commands are refused.",
                handler=self._scoped_run_command,
                input_schema={
                    "type": "object",
                    "properties": {
                        "cmd": {"type": "array", "items": {"type": "string"}},
                        "cwd": {"type": "string"},
                        "timeout": {"type": ["number", "null"]},
                    },
                    "required": ["cmd"],
                },
            ),
            RepairTool(
                name="read_file",
                description="Read a file's text content.",
                handler=self.read_file,
                input_schema={"type": "object", "properties": path_arg, "required": ["path"]},
            ),
            RepairTool(
                name="git_status",
                description="Read-only git status for the project.",
                handler=self.git_status,
                input_schema={"type": "object", "properties": project_arg, "required": ["project_dir"]},
            ),
            RepairTool(
                name="git_diff",
                description="Read-only git diff for the project.",
                handler=self.git_diff,
                input_schema={"type": "object", "properties": project_arg, "required": ["project_dir"]},
            ),
        ]


# --------------------------------------------------------------------------- fake impl


class FakeExecutor(Executor):
    """In-memory, scriptable executor for unit tests — no sandbox, no MCP.

    Script per-check outcomes by passing a sequence consumed in order. Each entry is either a
    ``bool`` (True == pass; a failing bool synthesizes a non-empty stderr) or a full
    :class:`CheckResult`. When a queue is exhausted, ``default_pass`` decides. This lets a test
    simulate "compile fails once then passes".
    """

    def __init__(
        self,
        *,
        compile_results: Sequence[bool | CheckResult] | None = None,
        build_results: Sequence[bool | CheckResult] | None = None,
        test_results: Sequence[bool | CheckResult] | None = None,
        lint_results: Sequence[bool | CheckResult] | None = None,
        default_pass: bool = True,
        files: dict[str, str] | None = None,
        status_text: str = "",
        diff_text: str = "",
        run_result: RunResult | None = None,
        install_result: RunResult | None = None,
    ) -> None:
        self._queues: dict[str, deque[bool | CheckResult]] = {
            "compile": deque(compile_results or []),
            "build": deque(build_results or []),
            "test": deque(test_results or []),
            "lint": deque(lint_results or []),
        }
        self._default_pass = default_pass
        self.files: dict[str, str] = {str(k): v for k, v in (files or {}).items()}
        self._status_text = status_text
        self._diff_text = diff_text
        self._run_result = run_result or RunResult(stdout="", stderr="", exit_code=0)
        self._install_result = install_result or RunResult(stdout="", stderr="", exit_code=0)
        self.commands: list[list[str]] = []
        self.installs: list[tuple[str, str, str]] = []
        self.commits: list[tuple[str, str]] = []
        self.writes: list[str] = []
        self._commit_seq = 0

    def _next_check(self, name: str) -> CheckResult:
        queue = self._queues[name]
        outcome: bool | CheckResult = queue.popleft() if queue else self._default_pass
        if isinstance(outcome, CheckResult):
            return outcome
        passed = bool(outcome)
        return CheckResult(
            name=name,
            passed=passed,
            stderr="" if passed else f"{name} failed (scripted)",
            exit_code=0 if passed else 1,
        )

    def run_command(self, cmd: Sequence[str], cwd: StrPath = ".", timeout: float | None = None) -> RunResult:
        self.commands.append(list(cmd))
        return self._run_result

    def write_file(self, path: StrPath, content: str) -> None:
        key = str(path)
        self.files[key] = content
        self.writes.append(key)

    def read_file(self, path: StrPath) -> str:
        try:
            return self.files[str(path)]
        except KeyError as exc:
            raise FileNotFoundError(str(path)) from exc

    def git_status(self, project_dir: StrPath) -> str:
        return self._status_text

    def git_diff(self, project_dir: StrPath) -> str:
        return self._diff_text

    def install_package(self, project_dir: StrPath, package: str, manager: str = "pip") -> RunResult:
        self.installs.append((str(project_dir), package, manager))
        return self._install_result

    def files_complete(self, project_dir: StrPath, target_files: Sequence[str]) -> CheckResult:
        missing = [p for p in target_files if f"{project_dir}/{p.lstrip('/')}" not in self.files]
        if missing:
            return CheckResult(name="files_complete", passed=False, stderr=f"missing required files: {', '.join(missing)}")
        return CheckResult(name="files_complete", passed=True)

    def compile(self, project_dir: StrPath) -> CheckResult:
        return self._next_check("compile")

    def build(self, project_dir: StrPath) -> CheckResult:
        return self._next_check("build")

    def test(self, project_dir: StrPath) -> CheckResult:
        return self._next_check("test")

    def lint(self, project_dir: StrPath) -> CheckResult:
        return self._next_check("lint")

    def git_commit(self, project_dir: StrPath, message: str) -> CommitResult:
        self._commit_seq += 1
        self.commits.append((str(project_dir), message))
        return CommitResult(committed=True, sha=f"fakesha{self._commit_seq:04d}")


# --------------------------------------------------------------------------- real impl


def _run_async(coro: Any) -> Any:
    """Run an awaitable to completion from sync code (bridges the sync Executor to async tools).

    Safe whether or not a loop is already running in the calling thread.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:  # inside a running loop: offload to a worker
        return pool.submit(lambda: asyncio.run(coro)).result()


def _as_dict(raw: Any) -> dict[str, Any]:
    """Coerce a tool result into a dict.

    Tool results arrive in several shapes depending on the MCP adapter version: a plain
    dict, a JSON string, or a LangChain/MCP content-block list like
    ``[{"type": "text", "text": "<json>", ...}]`` (langchain-mcp-adapters >=0.3). Unwrap a
    text content block to its ``text`` payload before parsing, so the real
    ``{stdout, stderr, exit_code, ...}`` isn't mistaken for the wrapper.
    """
    if isinstance(raw, dict):
        if isinstance(raw.get("text"), str) and raw.get("type") == "text":
            return _as_dict(raw["text"])
        return raw
    if isinstance(raw, (list, tuple)) and raw:
        return _as_dict(raw[0])
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {"stdout": raw, "stderr": "", "exit_code": 0, "timed_out": False}
        if isinstance(parsed, dict):
            return parsed
    return {"stdout": str(raw), "stderr": "", "exit_code": 0, "timed_out": False}


def _as_text(raw: Any) -> str:
    """Coerce a tool result into text (unwrapping a LangChain/MCP text content block)."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        if isinstance(raw.get("text"), str):
            return raw["text"]
        return str(raw)
    if isinstance(raw, (list, tuple)) and raw:
        return _as_text(raw[0])
    return str(raw)


class MCPExecutor(Executor):
    """Real executor backed by the exec-sandbox MCP server (Linux+bash, registry-scoped egress).

    Construct via :meth:`connect` (opens the client + fetches tools ONCE). Fixed-path checks map
    to direct ``tool.ainvoke`` calls with the right commands for the POC stack (python compile /
    ``tsc --noEmit`` / pip|npm build / pytest / ruff|eslint). The client is held for the process
    lifetime (opened in the FastAPI lifespan) — never opened/closed per request.
    """

    _REPAIR_PASSTHROUGH = ("install_package", "read_file", "git_status", "git_diff")

    def __init__(self, client: Any, tools: list[Any]) -> None:
        self._client = client
        self._tools: dict[str, Any] = {getattr(t, "name", ""): t for t in tools}

    @classmethod
    async def connect(cls, url: str, transport: str = "streamable_http") -> MCPExecutor:
        """Open a MultiServerMCPClient to the exec-sandbox and fetch its tools once."""
        from langchain_mcp_adapters.client import MultiServerMCPClient  # lazy: only in-file import

        connections = {"exec-sandbox": {"transport": transport, "url": url}}
        client = MultiServerMCPClient(connections)  # type: ignore[arg-type]
        tools = await client.get_tools()
        return cls(client, tools)

    async def aclose(self) -> None:
        """Release the client. Sessions are per-invocation here, so this is a no-op hook."""
        return None

    def _invoke(self, name: str, args: dict[str, Any]) -> Any:
        return _run_async(self._tools[name].ainvoke(args))

    # shared primitives
    def run_command(self, cmd: Sequence[str], cwd: StrPath = ".", timeout: float | None = None) -> RunResult:
        d = _as_dict(self._invoke("run_command", {"cmd": list(cmd), "cwd": str(cwd), "timeout": timeout}))
        return RunResult(
            stdout=str(d.get("stdout", "")),
            stderr=str(d.get("stderr", "")),
            exit_code=int(d.get("exit_code", -1)),
            timed_out=bool(d.get("timed_out", False)),
        )

    def write_file(self, path: StrPath, content: str) -> None:
        self._invoke("write_file", {"path": str(path), "content": content})

    def read_file(self, path: StrPath) -> str:
        return _as_text(self._invoke("read_file", {"path": str(path)}))

    def git_status(self, project_dir: StrPath) -> str:
        return _as_text(self._invoke("git_status", {"project_dir": str(project_dir)}))

    def git_diff(self, project_dir: StrPath) -> str:
        return _as_text(self._invoke("git_diff", {"project_dir": str(project_dir)}))

    def install_package(self, project_dir: StrPath, package: str, manager: str = "pip") -> RunResult:
        d = _as_dict(self._invoke("install_package", {"name": package, "manager": manager, "cwd": str(project_dir)}))
        return RunResult(
            stdout=str(d.get("stdout", "")),
            stderr=str(d.get("stderr", "")),
            exit_code=int(d.get("exit_code", -1)),
            timed_out=bool(d.get("timed_out", False)),
        )

    # fixed-path checks — map to the POC stack's real commands
    def _exists(self, project_dir: StrPath, rel: str) -> bool:
        return self.run_command(["test", "-f", rel], cwd=project_dir).exit_code == 0

    @staticmethod
    def _aggregate(name: str, results: list[tuple[str, RunResult]]) -> CheckResult:
        for label, run in results:
            if not run.ok:
                return CheckResult(
                    name=name, passed=False, stderr=f"[{label}] {run.stderr}",
                    stdout=run.stdout, exit_code=run.exit_code, timed_out=run.timed_out,
                )
        return CheckResult(name=name, passed=True)

    def files_complete(self, project_dir: StrPath, target_files: Sequence[str]) -> CheckResult:
        missing = [p for p in target_files if not self._exists(project_dir, p)]
        if missing:
            return CheckResult(name="files_complete", passed=False, stderr=f"missing required files: {', '.join(missing)}")
        return CheckResult(name="files_complete", passed=True)

    def _npm_install(self, project_dir: StrPath) -> RunResult:
        return self.run_command(["npm", "install", "--no-audit", "--no-fund"], cwd=project_dir)

    def compile(self, project_dir: StrPath) -> CheckResult:
        results = [("py", self.run_command(["python", "-m", "compileall", "-q", "."], cwd=project_dir))]
        if self._exists(project_dir, "tsconfig.json"):  # frontend
            # `tsc --noEmit` needs @types/* resolved to type-check JSX/imports at all — unlike
            # compileall (syntax-only, no import resolution), so node_modules must exist first.
            results.append(("npm-install", self._npm_install(project_dir)))
            results.append(("tsc", self.run_command(["npx", "tsc", "--noEmit"], cwd=project_dir)))
        return self._aggregate("compile", results)

    def build(self, project_dir: StrPath) -> CheckResult:
        results: list[tuple[str, RunResult]] = []
        if self._exists(project_dir, "requirements.txt"):
            results.append(("pip", self.run_command(
                ["python", "-m", "pip", "install", "--no-input", "--target", ".py_packages", "-r", "requirements.txt"],
                cwd=project_dir,
            )))
        if self._exists(project_dir, "package.json"):
            # Mirrors the pip branch above: install before building. Without this, `npm run
            # build` always fails on a fresh checkout (no node_modules) — a repro found by
            # running the real fixed checks against a real generated project (fixture-run-dev).
            results.append(("npm-install", self._npm_install(project_dir)))
            results.append(("npm", self.run_command(["npm", "run", "build", "--if-present"], cwd=project_dir)))
        return self._aggregate("build", results) if results else CheckResult(name="build", passed=True)

    def test(self, project_dir: StrPath) -> CheckResult:
        results = [("pytest", self.run_command(["python", "-m", "pytest", "-q"], cwd=project_dir))]
        if self._exists(project_dir, "package.json"):
            results.append(("npm-test", self.run_command(["npm", "test", "--if-present"], cwd=project_dir)))
        return self._aggregate("test", results)

    def lint(self, project_dir: StrPath) -> CheckResult:
        results = [("ruff", self.run_command(["python", "-m", "ruff", "check", "."], cwd=project_dir))]
        if self._exists(project_dir, "package.json"):
            results.append(("eslint", self.run_command(["npx", "eslint", "."], cwd=project_dir)))
        return self._aggregate("lint", results)

    def git_commit(self, project_dir: StrPath, message: str) -> CommitResult:
        d = _as_dict(self._invoke("git_commit", {"project_dir": str(project_dir), "message": message}))
        return CommitResult(
            committed=bool(d.get("committed", False)),
            sha=d.get("sha"),
            stdout=str(d.get("stdout", "")),
            stderr=str(d.get("stderr", "")),
            exit_code=int(d.get("exit_code", -1)),
        )

    # NOTE — deliberately NO publish_scaffold / publish_feature / publish_sweep here (unlike
    # LocalDiskExecutor). Those push to a git remote (github.com), and the exec-sandbox has no
    # route there BY DESIGN: its egress is locked to the PyPI + npm registries only
    # (tools/exec-sandbox/squid.conf), with no path to git hosts. Publishing/pushing is a
    # host-side concern. The publish-capable graph nodes (commit_node, feature_publish_node,
    # refactoring_publish_node, debug_publish_node) all gate on ``hasattr(executor, "publish_*")``
    # precisely so the sandbox path skips the push cleanly and falls back to a local git_commit —
    # it is NOT an oversight that these methods are absent. See debug_publish_node's docstring for
    # the operator-facing consequence + workaround.

    def _repair_run_command(self, cmd: list[str], cwd: str = ".", timeout: float | None = None) -> dict[str, Any]:
        """Scoped run_command exposed to the LLM (rejects git writes, then delegates)."""
        run = self._scoped_run_command(cmd, cwd, timeout)
        return {"stdout": run.stdout, "stderr": run.stderr, "exit_code": run.exit_code, "timed_out": run.timed_out}

    def get_repair_tools(self) -> list[Any]:
        """Real LangChain tools for the repair subset — bindable to the model via bind_tools.

        Excludes ``git_commit`` (rule 2) and swaps the raw ``run_command`` for a scoped wrapper
        that refuses git writes.
        """
        from langchain_core.tools import StructuredTool  # lazy: only in-file import

        tools: list[Any] = [self._tools[n] for n in self._REPAIR_PASSTHROUGH if n in self._tools]
        tools.append(
            StructuredTool.from_function(
                func=self._repair_run_command,
                name="run_command",
                description="Run a command in the sandbox. Scoped: git write commands are refused.",
            )
        )
        return tools


# --------------------------------------------------------------------------- provider

_active_executor: Executor | None = None


def set_executor(executor: Executor | None) -> None:
    """Register the process-wide executor (called once in the FastAPI lifespan)."""
    global _active_executor
    _active_executor = executor


def get_executor() -> Executor:
    """Return the active executor injected into the graph nodes."""
    if _active_executor is None:
        raise RuntimeError("no Executor configured — the exec-sandbox is not connected")
    return _active_executor
