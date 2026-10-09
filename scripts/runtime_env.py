"""The runtime a generated project is checked in, for ``LocalDiskExecutor`` (host-side, unsandboxed).

Lives beside ``local_executor.py`` in ``scripts/`` for the same reason: it starts processes and
containers on the host, which production code inside ``app/`` must never do.

* :func:`run_process` — run one command with a timeout that actually ends it. On Windows,
  ``subprocess.run(timeout=)`` kills only the direct child: ``npm.cmd`` dies, the ``node`` it
  started keeps the pipes open, and the call blocks until that grandchild finishes on its own (a
  real check took 62 minutes against ~33 minutes of timeouts). The whole process TREE is killed.
* :class:`PythonEnvs` — one ``uv`` virtualenv per generated backend, at the Python version the
  project itself targets (its Dockerfile's ``FROM python:3.11``, ``.python-version``,
  ``requires-python``), outside the project tree so nothing leaks into git, the zip or a listing.
  The tool's own interpreter (3.14 here) has no wheels yet for half of a typical backend's pins
  (``asyncpg`` failed to build in a real run), which no code edit could fix.
* :class:`ThrowawayPostgres` — a labelled Postgres container on a random localhost port with a
  random password, for the boot check and tests; removed at the end of the run, and swept by
  label at the start of the next one in case a run crashed. Its URL never touches the repo.
* :func:`probe` — wait for a started server to answer HTTP.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

_IMPL_DIR = Path(__file__).resolve().parent.parent
if str(_IMPL_DIR) not in sys.path:
    sys.path.insert(0, str(_IMPL_DIR))

from app.integrations.executor import RunResult, cap_output  # noqa: E402

_WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------- processes

def _popen_kwargs() -> dict:
    if _WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    """End ``proc`` and everything it started."""
    if proc.poll() is not None:
        return
    try:
        if _WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_process(
    argv: Sequence[str], cwd: str | Path, *, timeout: float | None = 120, env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> RunResult:
    """``subprocess.run`` with a timeout that ends the whole process tree, utf-8 decoding (the
    locale codepage loses any byte outside it), and capped output."""
    try:
        proc = subprocess.Popen(
            list(argv), cwd=str(cwd), env=env, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL, **_popen_kwargs(),
        )
    except FileNotFoundError as exc:
        return RunResult(stdout="", stderr=f"command not found: {argv[0]!r} ({exc})", exit_code=127)
    except OSError as exc:
        return RunResult(stdout="", stderr=f"could not start {argv[0]!r}: {exc}", exit_code=126)
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout or 120)
        return RunResult(stdout=cap_output(out or ""), stderr=cap_output(err or ""), exit_code=proc.returncode)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=15)
        except (subprocess.TimeoutExpired, ValueError):
            out, err = "", ""
        tail = (err or "").rstrip()
        return RunResult(stdout=cap_output(out or ""), stderr=cap_output((tail + "\n" if tail else "") + "[timed out]"),
                         exit_code=124, timed_out=True)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def file_hash(*paths: Path) -> str:
    h = hashlib.sha256()
    for path in paths:
        h.update(path.name.encode())
        h.update(path.read_bytes() if path.is_file() else b"<absent>")
    return h.hexdigest()


# --------------------------------------------------------------------------- Python environments

_FROM_PYTHON_RE = re.compile(r"^\s*FROM\s+(?:[\w./-]+/)?python:(\d+\.\d+)", re.IGNORECASE | re.MULTILINE)
_REQUIRES_PYTHON_RE = re.compile(r"requires-python\s*=\s*[\"'][^\"'\d]*(\d+\.\d+)")
_FALLBACK_PYTHON = "3.12"


def target_python(be_dir: Path) -> str | None:
    """The Python version a backend says it runs on, or ``None``."""
    for candidate in (be_dir / ".python-version", be_dir.parent / ".python-version"):
        if candidate.is_file():
            m = re.match(r"\s*(\d+\.\d+)", candidate.read_text(encoding="utf-8", errors="replace"))
            if m:
                return m.group(1)
    for dockerfile in (be_dir / "Dockerfile", be_dir.parent / "Dockerfile"):
        if dockerfile.is_file():
            m = _FROM_PYTHON_RE.search(dockerfile.read_text(encoding="utf-8", errors="replace"))
            if m:
                return m.group(1)
    pyproject = be_dir / "pyproject.toml"
    if pyproject.is_file():
        m = _REQUIRES_PYTHON_RE.search(pyproject.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1)
    runtime = be_dir / "runtime.txt"
    if runtime.is_file():
        m = re.search(r"(\d+\.\d+)", runtime.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1)
    return None


class PythonEnvs:
    """One ``uv`` virtualenv per backend directory, kept under ``<root>/.venvs/``."""

    TEST_TOOLS = ("pytest", "pytest-asyncio", "httpx")

    def __init__(self, root: Path, runner: Callable[..., RunResult] = run_process) -> None:
        self.base = root / ".venvs"
        self._run = runner
        self.uv = shutil.which("uv")

    def _python_of(self, venv: Path) -> Path:
        return venv / ("Scripts/python.exe" if _WINDOWS else "bin/python")

    def _key(self, be_dir: Path) -> str:
        """Short and unique: ``backend-1a2b3c4d5e``. A long name is not cosmetic on Windows — a
        venv path past ~260 characters makes installers fail outright (uv: "Trivial strip failed")."""
        resolved = str(be_dir.resolve())
        return f"{be_dir.name[:20]}-{hashlib.sha1(resolved.encode()).hexdigest()[:10]}"

    def python(self, be_dir: Path) -> tuple[str | None, list[tuple[str, RunResult]]]:
        """The backend's own interpreter (created on first use), or ``None`` when ``uv`` is not
        installed — the caller then falls back to the tool's interpreter, as before."""
        if not self.uv:
            return None, []
        venv = self.base / self._key(be_dir)
        python = self._python_of(venv)
        want = target_python(be_dir) or _FALLBACK_PYTHON
        marker = venv / ".sdlc-python"
        if python.exists() and marker.is_file() and marker.read_text(encoding="utf-8").strip() == want:
            return str(python), []
        results: list[tuple[str, RunResult]] = []
        tried = []
        for version in dict.fromkeys([want, _FALLBACK_PYTHON, f"{sys.version_info.major}.{sys.version_info.minor}"]):
            tried.append(version)
            res = self._run([self.uv, "venv", "--clear", "--python", version, str(venv)], self.base.parent, timeout=600)
            if res.ok:
                self.base.mkdir(parents=True, exist_ok=True)
                marker.write_text(want, encoding="utf-8")
                return str(python), results
            results.append((f"venv-{version}", res))     # "[venv-3.11[backend]]" parses as label[dir]
        return None, results

    def install(self, python: str, be_dir: Path) -> tuple[str, RunResult] | None:
        """``uv pip install -r requirements.txt`` into the backend's env, skipped when the
        requirements are unchanged since the last successful install."""
        req = be_dir / "requirements.txt"
        if not req.is_file() or not self.uv:
            return None
        marker = Path(python).parent.parent / ".sdlc-requirements"
        digest = file_hash(req)
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == digest:
            return None
        res = self._run([self.uv, "pip", "install", "--python", python, "-r", "requirements.txt"], be_dir, timeout=900)
        if res.ok:
            marker.write_text(digest, encoding="utf-8")
        return ("pip", res)

    def ensure_test_tools(self, python: str) -> RunResult | None:
        marker = Path(python).parent.parent / ".sdlc-test-tools"
        if marker.is_file() or not self.uv:
            return None
        res = self._run([self.uv, "pip", "install", "--python", python, *self.TEST_TOOLS], Path(python).parent, timeout=600)
        if res.ok:
            marker.write_text("ok", encoding="utf-8")
        return res


# --------------------------------------------------------------------------- throwaway Postgres

class ThrowawayPostgres:
    """A Postgres container that exists only for one run of checks."""

    LABEL = "sdlc.throwaway-db"
    USER = "app"
    DB = "app"

    def __init__(self, scope: str, *, image: str = "postgres:16-alpine",
                 runner: Callable[..., RunResult] = run_process) -> None:
        self.scope = re.sub(r"[^A-Za-z0-9._-]+", "_", scope)[-60:] or "run"
        self.image = image
        self._run = runner
        self.container: str | None = None
        self.port: int | None = None
        self.password = secrets.token_urlsafe(18)
        self.error = ""

    def _docker(self, *args: str, timeout: float = 120, input_text: str | None = None) -> RunResult:
        return self._run(["docker", *args], ".", timeout=timeout, input_text=input_text) if input_text is not None \
            else self._run(["docker", *args], ".", timeout=timeout)

    def sweep(self) -> None:
        """Remove containers a previous (crashed) run in this workspace left behind."""
        res = self._docker("ps", "-aq", "--filter", f"label={self.LABEL}={self.scope}", timeout=30)
        for cid in res.stdout.split() if res.ok else []:
            self._docker("rm", "-f", cid, timeout=60)

    def start(self) -> bool:
        if self.container:
            return True
        if not self._docker("version", "--format", "{{.Server.Version}}", timeout=20).ok:
            self.error = "Docker is not available"
            return False
        self.sweep()
        res = self._docker(
            "run", "-d", "--rm", "--label", f"{self.LABEL}={self.scope}",
            "-e", f"POSTGRES_USER={self.USER}", "-e", f"POSTGRES_PASSWORD={self.password}", "-e", f"POSTGRES_DB={self.DB}",
            "-p", "127.0.0.1::5432", self.image, timeout=300,
        )
        if not res.ok or not res.stdout.strip():
            self.error = f"could not start {self.image}: {(res.stderr or res.stdout).strip()[:300]}"
            return False
        self.container = res.stdout.strip().splitlines()[-1]
        mapped = self._docker("port", self.container, "5432/tcp", timeout=30)
        m = re.search(r":(\d+)\s*$", mapped.stdout.strip().splitlines()[0] if mapped.stdout.strip() else "")
        if not m:
            self.error = "could not read the database container's port"
            self.stop()
            return False
        self.port = int(m.group(1))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            ready = self._docker("exec", self.container, "pg_isready", "-U", self.USER, "-d", self.DB, "-h", "127.0.0.1",
                                 timeout=15)
            if ready.ok:
                return True
            time.sleep(1)
        self.error = "the database container did not become ready"
        self.stop()
        return False

    def reset(self) -> RunResult:
        """An empty ``public`` schema, so each boot applies the CURRENT migrations from scratch."""
        return self.psql("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")

    def psql(self, sql: str) -> RunResult:
        assert self.container
        return self._docker("exec", "-i", self.container, "psql", "-q", "-U", self.USER, "-d", self.DB,
                            "-v", "ON_ERROR_STOP=1", "-f", "-", timeout=120, input_text=sql)

    def url(self, scheme: str = "postgresql") -> str:
        return f"{scheme}://{self.USER}:{self.password}@127.0.0.1:{self.port}/{self.DB}"

    def stop(self) -> None:
        if self.container:
            self._docker("rm", "-f", self.container, timeout=60)
        self.container, self.port = None, None


#: Env keys that are part of a database connection, mapped to which value they get.
_DB_KEY_PARTS = (("PASSWORD", "password"), ("PASS", "password"), ("HOST", "host"), ("PORT", "port"),
                 ("USER", "user"), ("NAME", "name"), ("DATABASE", "name"))
_DB_KEY_RE = re.compile(r"^(?:DB|DATABASE|PG|POSTGRES(?:QL)?)_|_(?:DB|DATABASE)_", re.IGNORECASE)


def database_env(db: ThrowawayPostgres, declared: dict[str, str]) -> dict[str, str]:
    """Point every database variable the project declares at the throwaway container — in the
    URL scheme the project itself uses (``postgresql+asyncpg://`` stays async)."""
    out: dict[str, str] = {}
    for key, example in declared.items():
        if re.match(r"^\w*(?:DATABASE|DB|POSTGRES|PG)\w*_?URL$|^\w*_?(?:DATABASE|DB)_URI$", key, re.IGNORECASE):
            scheme = example.split("://", 1)[0] if "://" in example else "postgresql"
            if not scheme.startswith("postgres"):
                continue
            out[key] = db.url(scheme)
        elif _DB_KEY_RE.search(key):
            upper = key.upper()
            for part, field in _DB_KEY_PARTS:
                if upper.endswith(part):
                    out[key] = {"password": db.password, "host": "127.0.0.1", "port": str(db.port),
                                "user": db.USER, "name": db.DB}[field]
                    break
    if "DATABASE_URL" not in out and not any(k.endswith("URL") for k in out):
        out["DATABASE_URL"] = db.url("postgresql")
    return out


# --------------------------------------------------------------------------- env files & probing

_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_SECRET_KEY_RE = re.compile(r"SECRET|_KEY$|TOKEN|PASSWORD|SALT", re.IGNORECASE)


def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=value`` pairs of a ``.env``-style file (quotes and trailing comments removed)."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE_RE.match(line)
        if not m:
            continue
        value = m.group(2)
        if value[:1] in "'\"" and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value)
        out[m.group(1)] = value
    return out


def dev_values(declared: dict[str, str]) -> dict[str, str]:
    """A developer's ``cp .env.example .env``: every declared key, with a throwaway value for the
    secrets left blank (a settings class that requires ``SECRET_KEY`` would otherwise refuse to
    start, which says nothing about the code)."""
    out = dict(declared)
    for key, value in declared.items():
        if not value and _SECRET_KEY_RE.search(key):
            out[key] = "dev-" + secrets.token_hex(24)
    return out


def probe(port: int, proc: subprocess.Popen, *, timeout: float = 45, paths: Sequence[str] = ("/health", "/")) -> tuple[bool, str]:
    """Wait for the server on ``port`` to answer HTTP. ``(booted, detail)``: any status below 500
    counts — a 401 or a 404 still proves the app imported, configured itself and is serving."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False, f"the server exited with code {proc.returncode} before answering"
        for path in paths:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as resp:
                    return True, f"GET {path} -> {resp.status}"
            except urllib.error.HTTPError as exc:
                if exc.code < 500:
                    return True, f"GET {path} -> {exc.code}"
                body = exc.read(2000).decode("utf-8", "replace") if exc.fp else ""
                return False, f"GET {path} -> {exc.code}: {body}"
            except (urllib.error.URLError, ConnectionError, socket.timeout, OSError):
                break               # not listening yet
        time.sleep(0.5)
    return False, f"the server did not answer on port {port} within {int(timeout)}s"


def start_server(argv: Sequence[str], cwd: Path, env: dict[str, str]) -> tuple[subprocess.Popen | None, Path, str]:
    """Start a server with its output going to a temp file (a pipe nobody reads fills and blocks
    it). ``(process or None, log path, start error)``."""
    log = Path(tempfile.mkstemp(prefix="sdlc-boot-", suffix=".log")[1])
    handle = log.open("w", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(list(argv), cwd=str(cwd), env=env, stdout=handle, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, **_popen_kwargs())
    except (OSError, ValueError) as exc:
        handle.close()
        return None, log, f"could not start {argv[0]!r}: {exc}"
    handle.close()
    return proc, log, ""


def read_log(log: Path, limit: int = 12000) -> str:
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    finally:
        try:
            log.unlink()
        except OSError:
            pass
    return text[-limit:]
