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

import atexit
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

from app.integrations.executor import (  # noqa: E402
    CheckResult,
    CommitResult,
    Executor,
    RunResult,
    StrPath,
    aggregate_check_results,
    clean_listing,
)
from scripts.runtime_env import (  # noqa: E402
    PythonEnvs,
    ThrowawayPostgres,
    database_env,
    dev_values,
    file_hash,
    free_port,
    kill_tree,
    probe,
    read_env_file,
    read_log,
    run_process,
    start_server,
)

# On Windows, npm/npx ship as `.cmd` shims; `subprocess.run(["npm", ...])` without `shell=True`
# does NOT resolve the PATHEXT extension the way a shell would, and fails with "command not
# found" (WinError 2) even though `npm` is on PATH. `shutil.which` resolves the real executable
# (including its extension) on every platform; the bare name is a safe fallback on Unix, where
# this problem doesn't exist. Resolved once at import time — PATH doesn't change mid-process.
_NPM = shutil.which("npm") or "npm"
_NPX = shutil.which("npx") or "npx"

# Output capping (real incident: a project-wide `npm test` across 4000+ tests produced 7.5M chars
# of stderr, blowing the LLM's context outright) now lives in app.integrations.executor.cap_output
# — shared with MCPExecutor, which needs the exact same protection and previously lacked it.


def _global_git_identity() -> tuple[str, str]:
    """The developer's global git identity (user.name, user.email), or ('','') if unset."""
    def _g(key: str) -> str:
        r = subprocess.run(
            ["git", "config", "--global", key],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
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
        # The runtime the generated project is CHECKED in (scripts/runtime_env.py): its own Python
        # per backend, and a throwaway Postgres started on the first boot check.
        self._envs = PythonEnvs(self._root)
        self._db: ThrowawayPostgres | None = None
        self._db_tried = False
        self._db_error = ""
        #: The pack's own SQL DDL — applied to the throwaway database when the generated project has
        #: no migrations of its own to apply (debug_check_node sets it from the design package).
        self.schema_sql = ""
        # Every script that builds this executor gets the database removed at exit, crashed run
        # or not; a hard kill is covered by the label sweep the next run starts with.
        atexit.register(self.close)

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
        # Explicit utf-8/replace decoding (the Windows locale codepage loses any byte outside it,
        # silently emptying the captured output the Debugging agent's prompt is built from), and a
        # timeout that kills the whole process TREE — see runtime_env.run_process.
        return run_process(list(cmd), workdir, timeout=timeout or 120, env=env)

    def write_file(self, path: StrPath, content: str) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def read_file(self, path: StrPath) -> str:
        return self._resolve(path).read_text(encoding="utf-8")

    def list_files(self, project_dir: StrPath, prefix: str = "") -> list[str]:
        """``git ls-files`` when the project is a git repo, else a filesystem walk.

        Tracked-files-first matches the sandbox implementation AND honours the project's own
        .gitignore for free. The walk is the fallback for a tree that was written but not yet
        ``git init``-ed — which is every run before the first publish."""
        res = self.run_command(["git", "ls-files"], cwd=project_dir)
        if res.exit_code == 0 and res.stdout.strip():
            return clean_listing(res.stdout, prefix)
        try:
            root = self._resolve(project_dir)
            rels = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]
        except (OSError, ValueError):  # missing dir, or a path escaping the demo root
            return []
        return clean_listing("\n".join(rels), prefix)

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
            [sys.executable, "-m", "pip", "install", "--no-input", "--target", ".py_packages", package],
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

    #: Subdirectories never worth treating as a second manifest location — build output / VCS /
    #: dependency dirs, never a project root boilerplate.render_scaffold would emit a manifest into.
    _MANIFEST_SKIP_DIRS = frozenset({"node_modules", ".git", ".py_packages", "__pycache__", ".venv", "venv"})

    def _manifest_dirs(self, project_dir: StrPath, filename: str) -> list[str]:
        """``cwd``-ready directories (``project_dir`` itself, or one of its immediate
        subdirectories) that contain ``filename`` — the project root and EXACTLY one level down,
        never deeper.

        ``boilerplate.render_scaffold`` places a manifest either at the project root (a
        shared-root pack) or inside exactly one per-side wrapper directory it computes via
        ``_single_top_dir`` (e.g. ``auth-backend/requirements.txt``, ``auth-frontend/package.json``
        for a pack with separate backend/frontend roots) — never nested any deeper. Checking ONLY
        the root (the previous behavior) silently found nothing for a per-side-rooted project,
        which made ``build``/``test``/``compile``'s frontend half a no-op that still reported
        ``passed=True`` — the root cause of the Debugging/Unit-Test loop never actually compiling,
        building or running the generated tests for such a project.
        """
        base = self._resolve(project_dir)
        root = str(project_dir).replace("\\", "/").rstrip("/")
        found: list[str] = []
        if (base / filename).is_file():
            found.append(root)
        if base.is_dir():
            for child in sorted(base.iterdir()):
                if (
                    child.is_dir()
                    and child.name not in self._MANIFEST_SKIP_DIRS
                    and (child / filename).is_file()
                ):
                    found.append(f"{root}/{child.name}")
        return found

    def _npm_install(self, project_dir: StrPath) -> RunResult:
        """``npm install``, skipped when ``package.json`` / the lockfile are unchanged since the last
        successful install. compile() and build() both need node_modules and both ran a full
        install in every debugging round."""
        base = self._resolve(project_dir)
        marker = base / "node_modules" / ".sdlc-install-hash"
        digest = file_hash(base / "package.json", base / "package-lock.json")
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == digest:
            return RunResult(stdout="(dependencies unchanged since the last install)", stderr="", exit_code=0)
        res = self.run_command([_NPM, "install", "--no-audit", "--no-fund"], cwd=project_dir, timeout=600)
        if res.ok and (base / "node_modules").is_dir():
            # Hash AFTER the install: npm may have written the lockfile it just resolved.
            marker.write_text(file_hash(base / "package.json", base / "package-lock.json"), encoding="utf-8")
        return res

    def _backend_python(self, be_dir: StrPath) -> tuple[str | None, list[tuple[str, RunResult, str]]]:
        """The backend's own interpreter (its target Python, in a uv env outside the project), or
        ``None`` when uv is unavailable — then the tool's interpreter is used, as before."""
        python, setup = self._envs.python(self._resolve(be_dir))
        return python, [(f"{label}[{be_dir}]", res, str(be_dir)) for label, res in setup]

    def _py_packages_env(self, be_dir: StrPath) -> dict[str, str]:
        """Env with ``be_dir``'s ``.py_packages`` (where ``build()``'s ``pip install --target``
        lands the generated project's own backend dependencies) prepended to ``PYTHONPATH``.

        ``--target`` populates that directory but does NOT put it on ``sys.path`` for a later,
        separate process — without this, ``test()``'s pytest (run under THIS tool's own venv,
        via ``sys.executable``) can import only what the tool's venv happens to already have,
        and fails with ``ModuleNotFoundError`` on every dependency the generated project actually
        declared (e.g. ``python-jose``/``passlib``/``bcrypt`` for an auth backend) — masking a
        real compile/test outcome behind a false "missing dependency" failure.
        """
        pkg_dir = str(self._resolve(f"{be_dir}/.py_packages"))
        existing = os.environ.get("PYTHONPATH", "")
        pythonpath = f"{pkg_dir}{os.pathsep}{existing}" if existing else pkg_dir
        return {**os.environ, "PYTHONPATH": pythonpath}

    #: Testing-tool families that must never be resolved out of a generated project's own
    #: .py_packages: boilerplate.py's default scaffold unconditionally adds `pytest` to every
    #: backend's requirements.txt (for the generated README's own "pip install && pytest"
    #: instructions), so build()'s pip install would otherwise drop a SECOND, independently
    #: versioned pytest into .py_packages — and because PYTHONPATH entries resolve before the
    #: interpreter's own site-packages, _py_packages_env's PYTHONPATH would then make test()'s
    #: `sys.executable -m pytest` silently run under THAT shadow copy instead of the tool's own
    #: known-good pytest, version drift and all (confirmed: this venv has pytest 9.1.1, the
    #: default scaffold pins "pytest>=8.3,<9.0"). pytest is a dev/test tool the application code
    #: under test never imports, so removing it here costs the generated project nothing at
    #: runtime — `import pytest` in its test files still resolves fine via the tool's own venv.
    #: "_pytest" (leading underscore) is pytest's own implementation package — pytest's public
    #: `__init__.py` imports from it directly, so leaving a mismatched-version copy behind while
    #: only removing "pytest" itself produces a broken half-shadow (the host's newer pytest
    #: package trying to import a symbol only its own, now-absent _pytest provides).
    _SHADOW_EXCLUDE = ("pytest", "_pytest", "pluggy")

    def _strip_shadowing_packages(self, py_packages_dir: Path) -> None:
        """Remove any ``_SHADOW_EXCLUDE`` package build() just installed into ``.py_packages``."""
        if not py_packages_dir.is_dir():
            return
        for entry in py_packages_dir.iterdir():
            name = entry.name.lower()
            if name.startswith(self._SHADOW_EXCLUDE):
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)

    @staticmethod
    def _aggregate(name: str, results: list[tuple[str, RunResult, str]]) -> CheckResult:
        """Combine per-tool results into one CheckResult — every failing tool is reported.
        Single implementation shared by both executors: see ``app.integrations.executor.aggregate_check_results``."""
        return aggregate_check_results(name, results)

    def compile(self, project_dir: StrPath) -> CheckResult:
        results: list[tuple[str, RunResult, str]] = []
        backends = self._manifest_dirs(project_dir, "requirements.txt")
        for be_dir in backends:
            # Compiled by the backend's OWN Python: syntax valid on the tool's 3.14 can be invalid
            # on the 3.11 the project ships with, and the other way round.
            python, setup = self._backend_python(be_dir)
            results += setup
            results.append((f"py[{be_dir}]", self.run_command(
                [python or sys.executable, "-m", "compileall", "-q", "-x", r"[\\/](?:\.venv|\.py_packages|node_modules)[\\/]", "."],
                cwd=be_dir), str(be_dir)))
        if not backends:
            # compileall recurses from project_dir root, so it reaches Python files under a per-side
            # wrapper dir (e.g. auth-backend/) with no discovery needed — hence scope "".
            results.append(("py", self.run_command([sys.executable, "-m", "compileall", "-q", "."], cwd=project_dir), ""))
        for fe_dir in self._manifest_dirs(project_dir, "tsconfig.json"):  # frontend(s)
            # `tsc --noEmit` needs @types/* resolved to type-check JSX/imports at all — unlike
            # compileall (syntax-only, no import resolution), so node_modules must exist first.
            results.append((f"npm-install[{fe_dir}]", self._npm_install(fe_dir), fe_dir))
            results.append((f"tsc[{fe_dir}]", self.run_command([_NPX, "tsc", "--noEmit"], cwd=fe_dir, timeout=180), fe_dir))
        return self._aggregate("compile", results)

    def build(self, project_dir: StrPath) -> CheckResult:
        results: list[tuple[str, RunResult, str]] = []
        for be_dir in self._manifest_dirs(project_dir, "requirements.txt"):
            python, setup = self._backend_python(be_dir)
            results += setup
            if python:
                installed = self._envs.install(python, self._resolve(be_dir))
                if installed:
                    results.append((f"{installed[0]}[{be_dir}]", installed[1], be_dir))
                continue
            results.append((f"pip[{be_dir}]", self.run_command(
                [sys.executable, "-m", "pip", "install", "--no-input", "--target", ".py_packages", "-r", "requirements.txt"],
                cwd=be_dir, timeout=300,
            ), be_dir))
            self._strip_shadowing_packages(self._resolve(f"{be_dir}/.py_packages"))
        for node_dir in self._manifest_dirs(project_dir, "package.json"):
            # Mirrors the pip branch above: install before building. Without this, `npm run
            # build` always fails on a fresh checkout (no node_modules).
            results.append((f"npm-install[{node_dir}]", self._npm_install(node_dir), node_dir))
            results.append((f"npm[{node_dir}]", self.run_command([_NPM, "run", "build", "--if-present"], cwd=node_dir, timeout=300), node_dir))
        return self._aggregate("build", results) if results else CheckResult(name="build", passed=True)

    def test(self, project_dir: StrPath) -> CheckResult:
        results: list[tuple[str, RunResult, str]] = []
        for be_dir in self._manifest_dirs(project_dir, "requirements.txt"):
            # Gated (unlike the sandbox executor's unconditional pytest) — pytest exits 5 ("no
            # tests collected") on a project with zero Python files, which would otherwise mark a
            # pure-JS project's test check as failed.
            python, setup = self._backend_python(be_dir)
            results += setup
            if python:
                tools = self._envs.ensure_test_tools(python)
                if tools is not None and not tools.ok:
                    results.append((f"install[{be_dir}]", tools, be_dir))
                results.append((f"pytest[{be_dir}]", self.run_command(
                    [python, "-m", "pytest", "-q"], cwd=be_dir, timeout=300, env=self._runtime_env(be_dir),
                ), be_dir))
                continue
            results.append((f"pytest[{be_dir}]", self.run_command(
                [sys.executable, "-m", "pytest", "-q"], cwd=be_dir, timeout=180, env=self._py_packages_env(be_dir),
            ), be_dir))
        for node_dir in self._manifest_dirs(project_dir, "package.json"):
            results.append((f"npm-test[{node_dir}]", self.run_command(
                [_NPM, "test", "--if-present"], cwd=node_dir, timeout=300, env=self._runtime_env(node_dir)), node_dir))
        return self._aggregate("test", results) if results else CheckResult(name="test", passed=True)

    def lint(self, project_dir: StrPath) -> CheckResult:
        results: list[tuple[str, RunResult, str]] = []
        for be_dir in self._manifest_dirs(project_dir, "requirements.txt"):
            results.append((f"ruff[{be_dir}]", self.run_command([sys.executable, "-m", "ruff", "check", "."], cwd=be_dir, timeout=120), be_dir))
        for node_dir in self._manifest_dirs(project_dir, "package.json"):
            results.append((f"eslint[{node_dir}]", self.run_command([_NPX, "eslint", "."], cwd=node_dir, timeout=180), node_dir))
        return self._aggregate("lint", results) if results else CheckResult(name="lint", passed=True)

    # -- boot: does the app actually START? -----------------------------------

    _SERVER_PACKAGES = ("express", "fastify", "koa", "@hapi/hapi", "@nestjs/core", "restify")
    _DB_HINT_RE = re.compile(r"sqlalchemy|asyncpg|psycopg|databases|tortoise|\bpg\b|knex|sequelize|typeorm|prisma|"
                             r"postgres", re.IGNORECASE)

    def _declared_env(self, side_dir: StrPath) -> dict[str, str]:
        """What a developer's ``cp .env.example .env`` would give this side: the repo-root example,
        overridden by the side's own (``backend/.env.example``)."""
        side = self._resolve(side_dir)
        merged: dict[str, str] = {}
        for candidate in (side.parent / ".env.example", side / ".env.example"):
            merged.update(read_env_file(candidate))
        return merged

    def _needs_database(self, side_dir: StrPath, declared: dict[str, str]) -> bool:
        side = self._resolve(side_dir)
        manifests = " ".join(p.read_text(encoding="utf-8", errors="replace")
                             for p in (side / "requirements.txt", side / "package.json") if p.is_file())
        return any("postgres" in str(v).lower() for v in declared.values()) or bool(self._DB_HINT_RE.search(manifests))

    def _database(self) -> ThrowawayPostgres | None:
        """The run's throwaway Postgres, started on first use (None if Docker can't provide one)."""
        if self._db is None and not self._db_tried:
            self._db_tried = True
            db = ThrowawayPostgres(str(self._root))
            if db.start():
                self._db = db
            else:
                self._db_error = db.error
        return self._db

    def _runtime_env(self, side_dir: StrPath, **extra: str) -> dict[str, str]:
        """The environment a side's tests and boot run with: the declared variables (blank secrets
        given throwaway values), pointed at the throwaway database when the project uses one."""
        declared = self._declared_env(side_dir)
        env = {**os.environ, **dev_values(declared), "PYTHONUNBUFFERED": "1", **extra}
        if self._needs_database(side_dir, declared):
            db = self._database()
            if db is not None:
                env.update(database_env(db, declared))
        return env

    def _asgi_app(self, be_dir: StrPath) -> str | None:
        """``module:variable`` of the backend's ``FastAPI()`` (``app.main:app``), found the same way
        the reconcile pass resolves imports — from the real import roots."""
        from app.services.python_wiring import PyProject, _code

        base = self._resolve(be_dir)
        files = {}
        for path in base.rglob("*.py"):
            rel = path.relative_to(base).as_posix()
            if any(seg in self._MANIFEST_SKIP_DIRS or seg in ("tests", "migrations", "alembic") for seg in rel.split("/")[:-1]):
                continue
            files["be/" + rel] = path.read_text(encoding="utf-8", errors="replace")
        project = PyProject(files)
        found = []
        for path, text in files.items():
            m = re.search(r"^(\w+)[ \t]*(?::[ \t]*FastAPI[ \t]*)?=[ \t]*(?:FastAPI\(|create_app\(|create_application\()",
                          _code(text), re.MULTILINE)
            dotted = project.dotted_of(path) if m else None
            if m and dotted:
                found.append((not path.endswith(("/main.py", "/app.py")), path.count("/"), f"{dotted}:{m.group(1)}"))
        return sorted(found)[0][2] if found else None

    def _node_entry(self, node_dir: StrPath) -> list[str] | None:
        """``node <entry>`` for a Node SERVER (a package.json depending on a server framework)."""
        import json as _json

        base = self._resolve(node_dir)
        try:
            pkg = _json.loads((base / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        if not any(name in deps for name in self._SERVER_PACKAGES):
            return None
        start = str((pkg.get("scripts") or {}).get("start") or "")
        m = re.match(r"^\s*node\s+([^\s&|;]+)", start)
        candidates = [m.group(1)] if m else []
        candidates += [str(pkg["main"])] if pkg.get("main") else []
        candidates += ["src/server.js", "server.js", "src/index.js", "index.js", "src/app.js", "app.js"]
        for rel in candidates:
            if (base / rel).is_file():
                return ["node", rel]
        return None

    def _migrate(self, side_dir: StrPath, python: str | None, env: dict[str, str]) -> tuple[str, RunResult] | None:
        """Bring the throwaway database to the schema THIS code expects: the project's own
        migrations when it has them (a broken migration is a code failure), else the pack's DDL."""
        db = self._db
        if db is None:
            return None
        side = self._resolve(side_dir)
        db.reset()
        if python and (side / "alembic.ini").is_file():
            return ("migrate", self.run_command([python, "-m", "alembic", "upgrade", "head"], cwd=side_dir, timeout=180, env=env))
        if (side / "knexfile.js").is_file():
            return ("migrate", self.run_command([_NPX, "knex", "migrate:latest"], cwd=side_dir, timeout=180, env=env))
        if self.schema_sql:
            db.psql(self.schema_sql)       # the pack's DDL: its errors are the pack's, not the code's
        return None

    def boot(self, project_dir: StrPath) -> CheckResult:
        """Start each generated backend and wait for it to answer HTTP (any status below 500)."""
        results: list[tuple[str, RunResult, str]] = []
        targets: list[tuple[str, list[str] | None, str | None]] = []   # (dir, node argv, asgi app)
        for be_dir in self._manifest_dirs(project_dir, "requirements.txt"):
            app = self._asgi_app(be_dir)
            if app:
                targets.append((be_dir, None, app))
        for node_dir in self._manifest_dirs(project_dir, "package.json"):
            argv = self._node_entry(node_dir)
            if argv:
                targets.append((node_dir, argv, None))
        if not targets:
            return CheckResult(name="boot", passed=True, stdout="no server to boot (no FastAPI app or Node server found)")
        for side_dir, node_argv, app in targets:
            port = free_port()
            env = self._runtime_env(side_dir, PORT=str(port), HOST="127.0.0.1")
            needs_db = self._needs_database(side_dir, self._declared_env(side_dir))
            python = None
            if app:
                python, setup = self._backend_python(side_dir)
                results += setup
                argv = [python or sys.executable, "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", str(port)]
            else:
                argv = node_argv or []
            migrated = self._migrate(side_dir, python, env)
            if migrated and not migrated[1].ok:
                results.append((f"{migrated[0]}[{side_dir}]", migrated[1], side_dir))
                continue
            proc, log, error = start_server(argv, self._resolve(side_dir), env)
            if proc is None:
                results.append((f"boot[{side_dir}]", RunResult(stdout="", stderr=error, exit_code=127), side_dir))
                continue
            try:
                booted, detail = probe(port, proc, paths=("/health", "/openapi.json", "/") if app else ("/health", "/"))
            finally:
                kill_tree(proc)
                try:
                    proc.wait(timeout=15)
                except Exception:  # noqa: BLE001
                    pass
            output = read_log(log)
            if booted:
                results.append((f"boot[{side_dir}]", RunResult(stdout=f"booted: {detail}", stderr="", exit_code=0), side_dir))
                continue
            if needs_db and self._db is None:
                # No database could be provided here; a boot that needs one proves nothing.
                detail = f"[database unavailable] {getattr(self, '_db_error', '') or 'no database'}; {detail}"
            results.append((f"boot[{side_dir}]", RunResult(stdout=output, stderr=f"{' '.join(argv)}\n{detail}",
                                                         exit_code=1), side_dir))
        return self._aggregate("boot", results)

    def close(self) -> None:
        """Remove the run's throwaway database (scripts call this in their ``finally``)."""
        if self._db is not None:
            self._db.stop()
            self._db = None

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
