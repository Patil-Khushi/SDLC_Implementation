"""Ephemeral sandbox for the Code Review phase.

Code Review does STATIC analysis only — it never executes the project (Testing does that). But
to clone an untrusted, LLM-generated repo and run ruff/eslint/sonar-scanner over it, we do the
work inside a throwaway container that is created per review and destroyed after. This isolates
the one thing here that *does* execute code — eslint loading the repo's config — and keeps the
host clean.

Lifecycle (a context manager):

    with get_review_sandbox() as sb:
        sb.clone(repo_url)          # git clone the PUBLIC repo into the sandbox
        sb.run(["ruff", "check", "--output-format=json", "."])
        files = sb.list_files()     # tracked paths, for language detection + reading
        text  = sb.read_text(path)  # read a file's content (BEFORE the sandbox closes)
    # __exit__ tears the container down

Implementations:
* :class:`FakeReviewSandbox` — in-memory, scriptable; used by unit tests (no Docker).
* :class:`DockerReviewSandbox` — real; ``docker run`` a per-review container, ``docker exec`` the
  commands, ``docker rm -f`` on close. Needs a Docker daemon + the review image (see
  ``tools/review-sandbox/Dockerfile``). Unlike the code-gen exec-sandbox, this one is allowed
  egress to GitHub (clone) and SonarQube (upload); it does not need PyPI/npm lockdown because it
  never builds or runs the project.

Agents obtain a sandbox via :func:`get_review_sandbox` (or inject one in tests); they never build
the ``docker`` argv themselves.
"""

from __future__ import annotations

import shlex
import subprocess  # nosec B404 - used only to drive the `docker` CLI with fixed, non-shell argv
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence

from app.config.settings import get_settings
from app.integrations.executor import RunResult

# Where the repo is cloned inside the sandbox container.
_REPO_DIR = "/work/repo"


class ReviewSandbox(ABC):
    """A per-review, isolated workspace for cloning + static analysis. Use as a context manager."""

    def __enter__(self) -> "ReviewSandbox":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open(self) -> None:
        """Provision the sandbox (no-op for impls that need no setup)."""

    @abstractmethod
    def clone(self, repo_url: str, ref: str | None = None) -> RunResult:
        """Clone a PUBLIC repo into the sandbox (shallow). No credentials."""

    @abstractmethod
    def run(self, cmd: Sequence[str], timeout: float | None = None,
            env: dict[str, str] | None = None) -> RunResult:
        """Run ``cmd`` (argv) with the cloned repo as the working directory. ``env`` is passed as
        real process environment variables, never interpolated into ``cmd`` - secrets (e.g. a
        SonarQube token) reach the process this way instead of appearing in argv, where they'd be
        visible via the container's process list (``docker top`` / ``/proc/<pid>/cmdline``)."""

    @abstractmethod
    def read_text(self, rel_path: str) -> str:
        """Read a repo-relative file's text (raises on missing/unreadable)."""

    @abstractmethod
    def list_files(self) -> list[str]:
        """List repo-relative tracked file paths (for language detection + reading)."""

    @abstractmethod
    def close(self) -> None:
        """Destroy the sandbox. Safe to call more than once."""


# --------------------------------------------------------------------------- fake impl


class FakeReviewSandbox(ReviewSandbox):
    """In-memory, scriptable sandbox for unit tests — no Docker, no network.

    ``files`` seeds the cloned tree (repo-relative path -> content). ``responses`` maps a command
    keyword (matched against the argv, e.g. "ruff", "eslint", "sonar-scanner", "git") to the
    :class:`RunResult` to return; anything unmatched returns a clean default. Every call is
    recorded, and :attr:`closed` flips to True on teardown so tests can assert the lifecycle.
    """

    def __init__(
        self,
        *,
        files: dict[str, str] | None = None,
        responses: dict[str, RunResult] | Callable[[Sequence[str]], RunResult] | None = None,
        clone_result: RunResult | Sequence[RunResult] | None = None,
    ) -> None:
        self.files: dict[str, str] = dict(files or {})
        self._responses = responses
        if clone_result is None:
            self._clone_results = [RunResult(stdout="", stderr="", exit_code=0)]
        elif isinstance(clone_result, RunResult):
            self._clone_results = [clone_result]
        else:
            self._clone_results = list(clone_result)     # per-call results (for fallback tests)
        self._clone_i = 0
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.cloned: list[str] = []
        self.clone_refs: list[str | None] = []
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def clone(self, repo_url: str, ref: str | None = None) -> RunResult:
        self.cloned.append(repo_url)
        self.clone_refs.append(ref)
        result = self._clone_results[min(self._clone_i, len(self._clone_results) - 1)]
        self._clone_i += 1
        return result

    def run(self, cmd: Sequence[str], timeout: float | None = None,
            env: dict[str, str] | None = None) -> RunResult:
        argv = list(cmd)
        self.commands.append(argv)
        self.envs.append(dict(env or {}))
        if callable(self._responses):
            return self._responses(argv)
        if isinstance(self._responses, dict):
            joined = " ".join(argv)
            for keyword, result in self._responses.items():
                if keyword in joined:
                    return result
        return RunResult(stdout="", stderr="", exit_code=0)

    def read_text(self, rel_path: str) -> str:
        try:
            return self.files[rel_path]
        except KeyError as exc:
            raise FileNotFoundError(rel_path) from exc

    def list_files(self) -> list[str]:
        return sorted(self.files)

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- real impl


class DockerReviewSandbox(ReviewSandbox):
    """Real sandbox backed by a per-review Docker container.

    ``open`` starts a detached container from ``image`` (idling on ``sleep``); every operation is
    a ``docker exec`` into it; ``close`` force-removes it. The container is the isolation boundary
    for the clone + static analysis. Requires a reachable Docker daemon and the review image.
    """

    def __init__(self, image: str, *, timeout: float = 900.0, docker_bin: str = "docker") -> None:
        self._image = image
        self._timeout = timeout
        self._docker = docker_bin
        self._name = f"review-{uuid.uuid4().hex[:12]}"
        self._started = False

    def open(self) -> None:
        # Detached, idle container we exec into; --rm so a crash still cleans up eventually.
        # --add-host makes host.docker.internal resolve to the host (for reaching SonarQube on
        # Linux too; Docker Desktop provides it already).
        self._docker_cli(
            ["run", "-d", "--rm", "--name", self._name,
             "--add-host", "host.docker.internal:host-gateway",
             "--workdir", "/work", self._image,
             "sleep", str(int(self._timeout))]
        )
        self._started = True

    def clone(self, repo_url: str, ref: str | None = None) -> RunResult:
        git = ["git", "clone", "--depth", "1"]
        if ref:
            git += ["--branch", ref]
        git += [repo_url, _REPO_DIR]
        # `rm -rf` first so a retry (e.g. default-branch fallback) always has a clean target.
        script = f"rm -rf {shlex.quote(_REPO_DIR)} && {shlex.join(git)}"
        return self._exec(["sh", "-c", script], workdir="/work")

    def run(self, cmd: Sequence[str], timeout: float | None = None,
            env: dict[str, str] | None = None) -> RunResult:
        return self._exec(list(cmd), workdir=_REPO_DIR, timeout=timeout, env=env)

    def read_text(self, rel_path: str) -> str:
        result = self._exec(["cat", rel_path], workdir=_REPO_DIR)
        if not result.ok:
            raise FileNotFoundError(rel_path)
        return result.stdout or ""

    def list_files(self) -> list[str]:
        result = self._exec(["git", "ls-files"], workdir=_REPO_DIR)
        return [line for line in result.stdout.splitlines() if line.strip()]

    def close(self) -> None:
        if self._started:
            self._started = False
            try:
                self._docker_cli(["rm", "-f", self._name])
            except Exception:  # noqa: BLE001 - best-effort teardown; --rm is the backstop
                pass

    # -- docker plumbing ------------------------------------------------------

    def _exec(self, cmd: Sequence[str], *, workdir: str, timeout: float | None = None,
              env: dict[str, str] | None = None) -> RunResult:
        env_flags: list[str] = []
        for key, value in (env or {}).items():
            env_flags += ["-e", f"{key}={value}"]
        argv = [self._docker, "exec", *env_flags, "--workdir", workdir, self._name, *cmd]
        return self._invoke(argv, timeout=timeout or self._timeout)

    def _docker_cli(self, args: Sequence[str]) -> RunResult:
        return self._invoke([self._docker, *args], timeout=self._timeout)

    @staticmethod
    def _invoke(argv: Sequence[str], *, timeout: float) -> RunResult:
        try:
            proc = subprocess.run(  # nosec B603 - fixed argv, shell=False, no user-controlled binary
                list(argv), capture_output=True, text=True, errors="replace",
                timeout=timeout, check=False, shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(stdout=str(exc.stdout or ""), stderr=f"timed out: {shlex.join(argv)}",
                             exit_code=-1, timed_out=True)
        except FileNotFoundError as exc:
            return RunResult(stdout="", stderr=f"docker not available: {exc}", exit_code=-1)
        # Coerce None -> "" so callers never hit `len(None)` / None subscripting.
        return RunResult(stdout=proc.stdout or "", stderr=proc.stderr or "", exit_code=proc.returncode)


# --------------------------------------------------------------------------- provider


def get_review_sandbox() -> ReviewSandbox:
    """Build a Docker-backed sandbox from settings (the real backend for a live run)."""
    settings = get_settings()
    return DockerReviewSandbox(settings.review_sandbox_image, timeout=settings.review_sandbox_timeout)
