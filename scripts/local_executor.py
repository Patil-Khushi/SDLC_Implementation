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

Fixed-path checks: ``compile`` / ``build`` / ``test`` / ``lint`` run real commands on disk (same
shape as ``MCPExecutor``'s sandbox versions — npm/tsc/pytest/ruff/eslint), gated by which project
manifests exist, so a frontend-only or backend-only generated project only runs the steps that
apply to it. ``test``'s pytest step is gated on ``requirements.txt`` (not run unconditionally like
the sandbox executor's) because pytest exits 5 ("no tests collected") on a project with zero
Python files, which would otherwise mark a pure-JS project's test check as failed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

_IMPL_DIR = Path(__file__).resolve().parent.parent
if str(_IMPL_DIR) not in sys.path:
    sys.path.insert(0, str(_IMPL_DIR))

from app.integrations.executor import CheckResult, CommitResult, Executor, RunResult, StrPath

# On Windows, npm/npx ship as `.cmd` shims; `subprocess.run(["npm", ...])` without `shell=True`
# does NOT resolve the PATHEXT extension the way a shell would, and fails with "command not
# found" (WinError 2) even though `npm` is on PATH. `shutil.which` resolves the real executable
# (including its extension) on every platform; the bare name is a safe fallback on Unix, where
# this problem doesn't exist. Resolved once at import time — PATH doesn't change mid-process.
_NPM = shutil.which("npm") or "npm"
_NPX = shutil.which("npx") or "npx"


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

    def install_package(self, project_dir: StrPath, package: str, manager: str = "pip") -> RunResult:
        if manager == "npm":
            return self.run_command([_NPM, "install", "--no-audit", "--no-fund", package], cwd=project_dir)
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

    def _exists(self, project_dir: StrPath, rel: str) -> bool:
        return self._resolve(f"{project_dir}/{rel}").exists()

    def _npm_install(self, project_dir: StrPath) -> RunResult:
        return self.run_command([_NPM, "install", "--no-audit", "--no-fund"], cwd=project_dir, timeout=600)

    @staticmethod
    def _aggregate(name: str, results: list[tuple[str, RunResult]]) -> CheckResult:
        for label, run in results:
            if not run.ok:
                return CheckResult(
                    name=name, passed=False, stderr=f"[{label}] {run.stderr}",
                    stdout=run.stdout, exit_code=run.exit_code, timed_out=run.timed_out,
                )
        return CheckResult(name=name, passed=True)

    def compile(self, project_dir: StrPath) -> CheckResult:
        results = [("py", self.run_command(["python", "-m", "compileall", "-q", "."], cwd=project_dir))]
        if self._exists(project_dir, "tsconfig.json"):  # frontend
            # `tsc --noEmit` needs @types/* resolved to type-check JSX/imports at all — unlike
            # compileall (syntax-only, no import resolution), so node_modules must exist first.
            results.append(("npm-install", self._npm_install(project_dir)))
            results.append(("tsc", self.run_command([_NPX, "tsc", "--noEmit"], cwd=project_dir, timeout=180)))
        return self._aggregate("compile", results)

    def build(self, project_dir: StrPath) -> CheckResult:
        results: list[tuple[str, RunResult]] = []
        if self._exists(project_dir, "requirements.txt"):
            results.append(("pip", self.run_command(
                ["python", "-m", "pip", "install", "--no-input", "--target", ".py_packages", "-r", "requirements.txt"],
                cwd=project_dir, timeout=300,
            )))
        if self._exists(project_dir, "package.json"):
            # Mirrors the pip branch above: install before building. Without this, `npm run
            # build` always fails on a fresh checkout (no node_modules).
            results.append(("npm-install", self._npm_install(project_dir)))
            results.append(("npm", self.run_command([_NPM, "run", "build", "--if-present"], cwd=project_dir, timeout=300)))
        return self._aggregate("build", results) if results else CheckResult(name="build", passed=True)

    def test(self, project_dir: StrPath) -> CheckResult:
        results: list[tuple[str, RunResult]] = []
        if self._exists(project_dir, "requirements.txt"):
            # Gated (unlike the sandbox executor's unconditional pytest) — pytest exits 5 ("no
            # tests collected") on a project with zero Python files, which would otherwise mark a
            # pure-JS project's test check as failed.
            results.append(("pytest", self.run_command(["python", "-m", "pytest", "-q"], cwd=project_dir, timeout=180)))
        if self._exists(project_dir, "package.json"):
            results.append(("npm-test", self.run_command([_NPM, "test", "--if-present"], cwd=project_dir, timeout=300)))
        return self._aggregate("test", results) if results else CheckResult(name="test", passed=True)

    def lint(self, project_dir: StrPath) -> CheckResult:
        results: list[tuple[str, RunResult]] = []
        if self._exists(project_dir, "requirements.txt"):
            results.append(("ruff", self.run_command(["python", "-m", "ruff", "check", "."], cwd=project_dir, timeout=120)))
        if self._exists(project_dir, "package.json"):
            results.append(("eslint", self.run_command([_NPX, "eslint", "."], cwd=project_dir, timeout=180)))
        return self._aggregate("lint", results) if results else CheckResult(name="lint", passed=True)

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

    def commit_feature_history(
        self,
        project_dir: StrPath,
        *,
        scaffold_files: Sequence[str],
        feature_commits: Sequence[tuple[str, Sequence[str]]],
        base_branch: str = "main",
        feature_branch: str = "dev",
        push: bool = False,
        remote: str | None = None,
        token: str | None = None,
    ) -> CommitResult:
        """Build a real branch history: the scaffold on ``base_branch`` (main), then ONE commit
        per feature on ``feature_branch`` (dev). Deterministic fixed-path git — never formed by
        the LLM (CLAUDE.md rule 2). This is what gives the generated repo a per-feature history
        instead of a single squashed commit.

        ``scaffold_files`` and each feature's paths are repo-root-relative (e.g.
        ``quickbite-backend/src/app.js``); missing paths are skipped and a final catch-all commit
        sweeps up anything written but not listed.

        When ``push`` and ``remote`` are set, the mandatory push workflow (rules 4 & 8) runs
        INTERLEAVED: ``base_branch`` is pushed right after the scaffold commit, and
        ``feature_branch`` is pushed immediately after EACH feature commit — the next feature is
        committed only after the previous push succeeds, and the FIRST push failure stops the run
        (returns ``exit_code=1``). With ``push=False`` (the default) it commits locally and pushes
        nothing. ``remote`` is a GitHub ``owner/name`` slug (created via ``gh`` if absent) or any
        git remote URL/path (used directly — e.g. a local bare repo in tests).
        """
        root = self._resolve(project_dir)
        root.mkdir(parents=True, exist_ok=True)
        if not (root / ".git").is_dir():
            self.run_command(["git", "init"], cwd=project_dir)

        env = {**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token} if token else None

        def _git(*args: str) -> RunResult:
            return self.run_command(["git", *self._identity, *args], cwd=project_dir)

        def _checkout(branch: str) -> None:
            exists = self.run_command(
                ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=project_dir
            ).exit_code == 0
            _git("checkout", branch) if exists else _git("checkout", "-B", branch)

        def _staged() -> bool:
            return self.run_command(["git", "diff", "--cached", "--quiet"], cwd=project_dir).exit_code != 0

        def _add(paths: Sequence[str]) -> None:
            existing = [p for p in paths if (root / p).exists()]
            if existing:
                _git("add", "--", *existing)

        # -- push helpers (only exercised when push + remote); origin is set up once, lazily --
        origin_ready = {"done": False}

        def _ensure_origin() -> None:
            if origin_ready["done"]:
                return
            self.run_command(["git", "remote", "remove", "origin"], cwd=project_dir)  # ignore if absent
            if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", remote or ""):  # GitHub owner/name slug
                self.run_command(["gh", "auth", "setup-git"], cwd=project_dir, env=env)
                vis = "--private" if self._private else "--public"
                # create the repo if it doesn't exist yet (ignored if it already does)
                self.run_command(
                    ["gh", "repo", "create", remote, vis, "--source", ".", "--remote", "origin"],
                    cwd=project_dir, env=env,
                )
                self.run_command(["git", "remote", "remove", "origin"], cwd=project_dir)  # normalize
                self.run_command(
                    ["git", "remote", "add", "origin", f"https://github.com/{remote}.git"], cwd=project_dir
                )
            else:  # a URL or local path (e.g. a bare repo in tests) — use directly
                self.run_command(["git", "remote", "add", "origin", remote], cwd=project_dir)
            origin_ready["done"] = True

        def _push(branch: str) -> RunResult:
            _ensure_origin()
            return self.run_command(["git", "push", "-u", "origin", branch], cwd=project_dir, env=env)

        pushing = bool(push and remote)
        commits = 0
        push_err = ""

        # main: scaffold ONLY (rule 3) + push main (rule 4)
        _checkout(base_branch)
        _add(list(scaffold_files))
        if _staged():
            _git("commit", "-m", "chore: initial project scaffold")
            commits += 1
        if pushing:
            r = _push(base_branch)
            if not r.ok:
                push_err = f"push {base_branch}: {(r.stderr or r.stdout).strip()[:200]}"

        # dev: one commit per feature, EACH pushed immediately (rule 8); stop on first failure
        if not push_err:
            _checkout(feature_branch)
            for message, paths in feature_commits:
                _add(list(paths))
                if _staged():
                    _git("commit", "-m", message)
                    commits += 1
                if pushing:
                    r = _push(feature_branch)
                    if not r.ok:  # next feature only after this push succeeds
                        push_err = f"push {feature_branch} ({message}): {(r.stderr or r.stdout).strip()[:200]}"
                        break
            # sweep up any file written but not listed in a feature (extras the model produced)
            if not push_err:
                _git("add", "-A")
                if _staged():
                    _git("commit", "-m", "chore: remaining generated files")
                    commits += 1
                    if pushing:
                        r = _push(feature_branch)
                        if not r.ok:
                            push_err = f"push {feature_branch} (sweep): {(r.stderr or r.stdout).strip()[:200]}"

        sha = self.run_command(["git", "rev-parse", "HEAD"], cwd=project_dir).stdout.strip() or None
        if push_err:
            return CommitResult(
                committed=commits > 0, sha=sha,
                stdout=f"{commits} commit(s) on {base_branch}/{feature_branch}; PUSH FAILED",
                stderr=push_err, exit_code=1,
            )
        note = f"; pushed to {remote}" if pushing else ""
        return CommitResult(
            committed=commits > 0, sha=sha,
            stdout=f"{commits} commit(s) on {base_branch}/{feature_branch}{note}", exit_code=0,
        )

    # -- incremental live publish (repo appears early + per-feature pushes) --------------------

    def _git(self, project_dir: StrPath, *args: str, env: dict[str, str] | None = None) -> RunResult:
        """git with the configured commit identity, in ``project_dir``."""
        return self.run_command(["git", *self._identity, *args], cwd=project_dir, env=env)

    def _pat_env(self, token: str | None) -> dict[str, str] | None:
        """Child env carrying the PAT (so gh/git push authenticate as it); None when no token."""
        return {**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token} if token else None

    def _has_staged(self, project_dir: StrPath) -> bool:
        return self.run_command(["git", "diff", "--cached", "--quiet"], cwd=project_dir).exit_code != 0

    def publish_scaffold(
        self, project_dir: StrPath, scaffold_files: Sequence[str], *,
        base_branch: str = "main", remote: str, token: str | None = None,
    ) -> RunResult:
        """EARLY publish: commit the scaffold on ``base_branch``, create the GitHub repo, and push
        ``base_branch`` — so the repo appears on GitHub BEFORE any feature is generated. ``remote``
        is a GitHub ``owner/name`` slug (created via ``gh`` if absent) or a URL/path used directly.
        Returns the push RunResult (exit_code 0 = pushed)."""
        root = self._resolve(project_dir)
        root.mkdir(parents=True, exist_ok=True)
        if not (root / ".git").is_dir():
            self.run_command(["git", "init"], cwd=project_dir)
        env = self._pat_env(token)
        self._git(project_dir, "checkout", "-B", base_branch)
        existing = [p for p in scaffold_files if (root / p).exists()]
        if existing:
            self._git(project_dir, "add", "--", *existing)
        if self._has_staged(project_dir):
            self._git(project_dir, "commit", "-m", "chore: initial project scaffold")
        self.run_command(["git", "remote", "remove", "origin"], cwd=project_dir)  # ignore if absent
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", remote or ""):  # owner/name slug
            self.run_command(["gh", "auth", "setup-git"], cwd=project_dir, env=env)
            vis = "--private" if self._private else "--public"
            self.run_command(
                ["gh", "repo", "create", remote, vis, "--source", ".", "--remote", "origin"],
                cwd=project_dir, env=env,
            )
            self.run_command(["git", "remote", "remove", "origin"], cwd=project_dir)  # normalize
            self.run_command(
                ["git", "remote", "add", "origin", f"https://github.com/{remote}.git"], cwd=project_dir
            )
        else:  # a URL or local path (e.g. a bare repo in tests) — use directly
            self.run_command(["git", "remote", "add", "origin", remote], cwd=project_dir)
        return self.run_command(["git", "push", "-u", "origin", base_branch], cwd=project_dir, env=env)

    def publish_feature(
        self, project_dir: StrPath, message: str, paths: Sequence[str], *,
        feature_branch: str = "dev", base_branch: str = "main", token: str | None = None,
    ) -> RunResult:
        """Commit ``paths`` as ONE feature commit on ``feature_branch`` and push it — called per work
        item as it completes, so features stream to GitHub live. Assumes ``publish_scaffold`` already
        created the repo + origin. Returns the push RunResult."""
        env = self._pat_env(token)
        exists = self.run_command(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{feature_branch}"], cwd=project_dir
        ).exit_code == 0
        if exists:
            self._git(project_dir, "checkout", feature_branch)
        else:  # first feature — branch dev off the scaffold on main
            self._git(project_dir, "checkout", "-B", feature_branch)
        root = self._resolve(project_dir)
        existing = [p for p in paths if (root / p).exists()]
        if existing:
            self._git(project_dir, "add", "--", *existing)
        if self._has_staged(project_dir):
            self._git(project_dir, "commit", "-m", message)
        return self.run_command(["git", "push", "-u", "origin", feature_branch], cwd=project_dir, env=env)

    def publish_sweep(
        self, project_dir: StrPath, *, feature_branch: str = "dev", token: str | None = None,
    ) -> RunResult:
        """Final catch-all: commit + push any files written but not captured by a per-feature push
        (extras the model produced). No-op if the tree is clean. Returns the push (or no-op) result."""
        env = self._pat_env(token)
        self._git(project_dir, "checkout", feature_branch)
        self._git(project_dir, "add", "-A")
        if not self._has_staged(project_dir):
            return RunResult(stdout="nothing to sweep", stderr="", exit_code=0)
        self._git(project_dir, "commit", "-m", "chore: remaining generated files")
        return self.run_command(["git", "push", "-u", "origin", feature_branch], cwd=project_dir, env=env)

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
            # `gh repo create --push` pushes only the CURRENT branch; push every other branch
            # (e.g. main + dev) so the full feature history lands on the remote.
            self.run_command(["git", "push", "origin", "--all"], cwd=project_dir, env=env)
            return created
        # fallback: repo already exists on GitHub → set the remote explicitly and push all branches
        self.run_command(["git", "remote", "remove", "origin"], cwd=project_dir)
        self.run_command(["git", "remote", "add", "origin", f"https://github.com/{repo}.git"], cwd=project_dir)
        return self.run_command(["git", "push", "-u", "origin", "--all"], cwd=project_dir, env=env)
