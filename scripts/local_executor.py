"""LocalDiskExecutor — a real-filesystem, real-git Executor for the local demo.

Unlike the production ``MCPExecutor`` (which runs everything inside the locked-down exec-sandbox
container), this writes generated files to a real folder on your machine and runs real ``git``
there. It exists ONLY for the demo server's / runner's ``--real`` mode so you can watch actual
Claude output land on disk, get committed, and (optionally) get pushed to a real GitHub repo —
without needing Docker.

It deliberately lives under ``scripts/`` (not ``app/integrations/``) because it bypasses the
sandbox boundary that CLAUDE.md rule 6 requires of production code — running unsandboxed commands
on the host is fine for a single-user local demo, but is NOT how the real service executes.

Git identity: commits are attributed to the developer's global ``git config`` identity
(``user.name`` / ``user.email``), falling back to a local demo identity only if none is set.

Publishing: when constructed with ``publish_repo="owner/name"``, ``git_commit`` will ALSO — after
the local commit — create that GitHub repo via the ``gh`` CLI (which must be authenticated) and
push. This is how "the agent creates the repo when the code is done" works: the commit step is
reached only after the completeness gate passes and the human approves, so the push happens on
completion + approval.

Gate note: the code-generation gate is completeness-only (``files_complete``), so ``compile`` /
``build`` / ``test`` / ``lint`` are never invoked here — they return a no-op pass.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

_IMPL_DIR = Path(__file__).resolve().parent.parent
if str(_IMPL_DIR) not in sys.path:
    sys.path.insert(0, str(_IMPL_DIR))

from app.integrations.executor import CheckResult, CommitResult, Executor, RunResult, StrPath


def _global_git_identity() -> tuple[str, str]:
    """The developer's global git identity (user.name, user.email), or ('','') if unset."""
    def _g(key: str) -> str:
        r = subprocess.run(["git", "config", "--global", key], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    return _g("user.name"), _g("user.email")


class LocalDiskExecutor(Executor):
    """Writes to ``root/<path>`` on the real filesystem; commits (and optionally publishes) with real git."""

    def __init__(self, root: StrPath, *, publish_repo: str | None = None, private: bool = True) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._publish_repo = publish_repo   # "owner/name" -> create on GitHub + push after commit
        self._private = private
        self._dev_name, self._dev_email = _global_git_identity()
        # Commit as the developer if their global identity is set; else a clearly-marked fallback.
        name = self._dev_name or "imp-001-demo"
        email = self._dev_email or "demo@local"
        self._identity = ["-c", f"user.email={email}", "-c", f"user.name={name}"]

    @property
    def root(self) -> Path:
        return self._root

    def _resolve(self, rel: StrPath) -> Path:
        target = (self._root / str(rel)).resolve()
        if target != self._root and self._root not in target.parents:
            raise ValueError(f"path escapes demo root: {rel!r}")
        return target

    # -- shared primitives ---------------------------------------------------

    def run_command(
        self,
        cmd: Sequence[str],
        cwd: StrPath = ".",
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> RunResult:
        workdir = self._resolve(cwd)
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                list(cmd), cwd=str(workdir), capture_output=True, text=True, timeout=timeout or 120, env=env
            )
            return RunResult(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)
        except subprocess.TimeoutExpired:
            return RunResult(stdout="", stderr="[timed out]", exit_code=124, timed_out=True)
        except FileNotFoundError as exc:
            return RunResult(stdout="", stderr=f"command not found: {cmd[0]!r} ({exc})", exit_code=127)

    def write_file(self, path: StrPath, content: str) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def read_file(self, path: StrPath) -> str:
        return self._resolve(path).read_text(encoding="utf-8")

    def git_status(self, project_dir: StrPath) -> str:
        r = self.run_command(["git", "status", "--porcelain"], cwd=project_dir)
        return r.stdout + r.stderr

    def git_diff(self, project_dir: StrPath) -> str:
        r = self.run_command(["git", "diff"], cwd=project_dir)
        return r.stdout + r.stderr

    def install_package(self, project_dir: StrPath, package: str) -> RunResult:
        return self.run_command(
            ["python", "-m", "pip", "install", "--no-input", "--target", ".py_packages", package],
            cwd=project_dir,
        )

    # -- fixed-path checks ---------------------------------------------------

    def files_complete(self, project_dir: StrPath, target_files: Sequence[str]) -> CheckResult:
        missing = [p for p in target_files if not self._resolve(f"{project_dir}/{p.lstrip('/')}").exists()]
        if missing:
            return CheckResult(name="files_complete", passed=False, stderr=f"missing required files: {', '.join(missing)}")
        return CheckResult(name="files_complete", passed=True)

    # compile/build/test/lint are NOT part of the completeness-only gate — no-op pass.
    def compile(self, project_dir: StrPath) -> CheckResult:
        return CheckResult(name="compile", passed=True, stdout="(compile not run: completeness-only gate)")

    def build(self, project_dir: StrPath) -> CheckResult:
        return CheckResult(name="build", passed=True, stdout="(build not run: completeness-only gate)")

    def test(self, project_dir: StrPath) -> CheckResult:
        return CheckResult(name="test", passed=True, stdout="(test not run here)")

    def lint(self, project_dir: StrPath) -> CheckResult:
        return CheckResult(name="lint", passed=True, stdout="(lint not run here)")

    # -- commit (real git) + optional publish to GitHub (real push) ----------

    def git_commit(self, project_dir: StrPath, message: str) -> CommitResult:
        # Force an ISOLATED repo for the generated project. Using `rev-parse --is-inside-work-tree`
        # is WRONG when the project dir is nested inside another repo (e.g. the tooling repo): it
        # returns true for the PARENT, so git would commit/push against the parent instead. Require
        # a `.git` *in this dir*; if absent, init a fresh nested repo that shadows the parent.
        if not (self._resolve(project_dir) / ".git").is_dir():
            self.run_command(["git", "init"], cwd=project_dir)
        add = self.run_command(["git", *self._identity, "add", "-A"], cwd=project_dir)
        if add.exit_code != 0:
            return CommitResult(committed=False, stdout=add.stdout, stderr=add.stderr, exit_code=add.exit_code)
        commit = self.run_command(["git", *self._identity, "commit", "-m", message], cwd=project_dir)
        sha = None
        push_note = ""
        if commit.exit_code == 0:
            sha = self.run_command(["git", "rev-parse", "HEAD"], cwd=project_dir).stdout.strip() or None
            if self._publish_repo:
                pub = self.publish(project_dir, self._publish_repo, private=self._private)
                push_note = f"\n[publish {self._publish_repo}] exit={pub.exit_code}\n{pub.stdout}\n{pub.stderr}"
        return CommitResult(
            committed=commit.exit_code == 0, sha=sha,
            stdout=commit.stdout + push_note, stderr=commit.stderr, exit_code=commit.exit_code,
        )

    def publish(
        self, project_dir: StrPath, repo: str, *, private: bool = True, token: str | None = None
    ) -> RunResult:
        """Create ``repo`` (owner/name) on GitHub via the ``gh`` CLI and push. Idempotent-ish.

        Reattributes the HEAD commit to the developer identity if it was made by the fallback
        identity, so the pushed history is owned by the developer's account.

        Auth: normally uses the authenticated ``gh`` CLI (``gh auth status``). When ``token`` (a
        Personal Access Token) is given, gh AND the git push run as that token's owner — the token
        is passed only via ``GH_TOKEN``/``GITHUB_TOKEN`` in the child env (never written to argv or
        ``.git/config``), so you can publish under an account the local ``gh`` isn't logged into.
        """
        # Carry the PAT to child gh/git processes via env only; git's credential helper
        # (configured by `gh auth setup-git`) resolves GH_TOKEN, so nothing lands on disk.
        env = {**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token} if token else None

        # 1) ensure the developer identity on this repo + reattribute HEAD if needed
        if self._dev_name and self._dev_email:
            self.run_command(["git", "config", "user.name", self._dev_name], cwd=project_dir)
            self.run_command(["git", "config", "user.email", self._dev_email], cwd=project_dir)
            head_email = self.run_command(["git", "log", "-1", "--format=%ae"], cwd=project_dir).stdout.strip()
            if head_email and head_email != self._dev_email:
                self.run_command(["git", "commit", "--amend", "--reset-author", "--no-edit"], cwd=project_dir)

        # 2) let gh act as the git credential helper for github.com (token flows via env)
        self.run_command(["gh", "auth", "setup-git"], cwd=project_dir, env=env)

        # 3) point origin at the INTENDED repo ONLY — never reuse an inherited/foreign origin
        #    (removing first is a no-op when absent). Then create the repo on GitHub and push.
        self.run_command(["git", "remote", "remove", "origin"], cwd=project_dir)
        vis = "--private" if private else "--public"
        created = self.run_command(
            ["gh", "repo", "create", repo, vis, "--source", ".", "--remote", "origin", "--push"],
            cwd=project_dir,
            env=env,
        )
        if created.exit_code == 0:
            return created
        # fallback: repo already exists on GitHub → set the remote explicitly and push
        self.run_command(["git", "remote", "remove", "origin"], cwd=project_dir)
        self.run_command(["git", "remote", "add", "origin", f"https://github.com/{repo}.git"], cwd=project_dir)
        return self.run_command(["git", "push", "-u", "origin", "HEAD"], cwd=project_dir, env=env)
