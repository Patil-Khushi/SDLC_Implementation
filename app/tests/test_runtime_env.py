"""The runtime generated code is checked in (``scripts/runtime_env.py`` + ``LocalDiskExecutor``).

Real processes where it is cheap and decisive: a timed-out command must end its whole process
tree (on Windows ``subprocess.run`` kills only ``npm.cmd`` and blocks until the ``node`` it started
finishes), the boot probe must tell "serving" from "crashed", and a real FastAPI app must boot — or
fail with its own traceback. Docker is exercised through a scripted runner, plus one integration
test that runs only where a Docker daemon is present.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.local_executor import LocalDiskExecutor
from scripts.runtime_env import (
    PythonEnvs,
    RunResult,
    ThrowawayPostgres,
    database_env,
    dev_values,
    free_port,
    kill_tree,
    probe,
    read_env_file,
    run_process,
    start_server,
    target_python,
)


# --------------------------------------------------------------- processes

def test_a_timeout_ends_the_whole_process_tree(tmp_path: Path):
    grandchild = "import time; time.sleep(120)"
    child = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {grandchild!r}])"
    started = time.monotonic()
    res = run_process([sys.executable, "-c", child], tmp_path, timeout=3)
    assert res.timed_out and res.exit_code == 124 and res.stderr.endswith("[timed out]")
    assert time.monotonic() - started < 40          # not the grandchild's 120 s


def test_a_missing_command_is_exit_127(tmp_path: Path):
    assert run_process(["definitely-not-a-command-xyz"], tmp_path).exit_code == 127


def test_utf8_output_survives(tmp_path: Path):
    res = run_process([sys.executable, "-c", "import sys; sys.stdout.buffer.write('café — ok'.encode())"], tmp_path)
    assert res.stdout == "café — ok"


def test_the_probe_tells_serving_from_crashed(tmp_path: Path):
    port = free_port()
    proc, log, err = start_server([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"], tmp_path, None)
    try:
        booted, detail = probe(port, proc, timeout=20, paths=("/",))
        assert booted and detail.startswith("GET / -> 200")
    finally:
        kill_tree(proc)
    crash, _log, _ = start_server([sys.executable, "-c", "raise SystemExit(3)"], tmp_path, None)
    booted, detail = probe(free_port(), crash, timeout=10)
    assert not booted and "exited with code 3" in detail


# --------------------------------------------------------------- python version, env files

def test_the_target_python_comes_from_the_project(tmp_path: Path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.11-slim AS builder\n", encoding="utf-8")
    assert target_python(tmp_path) == "3.11"
    (tmp_path / ".python-version").write_text("3.12.4\n", encoding="utf-8")
    assert target_python(tmp_path) == "3.12"                      # an explicit pin wins
    other = tmp_path / "x"
    other.mkdir()
    (other / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.10"\n', encoding="utf-8")
    assert target_python(other) == "3.12"                         # .python-version one level up
    empty = tmp_path / "e" / "f"
    empty.mkdir(parents=True)
    assert target_python(empty) is None                           # nothing stated: the caller's default


def test_env_files_and_dev_values(tmp_path: Path):
    path = tmp_path / ".env.example"
    path.write_text("# comment\nDATABASE_URL=postgresql+asyncpg://u:p@localhost/x\nexport PORT=8000 # trailing\n"
                    'NAME="quoted value"\nSECRET_KEY=\nJWT_SECRET_KEY=\n', encoding="utf-8")
    declared = read_env_file(path)
    assert declared == {"DATABASE_URL": "postgresql+asyncpg://u:p@localhost/x", "PORT": "8000",
                        "NAME": "quoted value", "SECRET_KEY": "", "JWT_SECRET_KEY": ""}
    values = dev_values(declared)
    assert values["SECRET_KEY"].startswith("dev-") and values["JWT_SECRET_KEY"].startswith("dev-")
    assert values["PORT"] == "8000"


def test_venv_names_stay_short_on_deep_paths(tmp_path: Path):
    deep = tmp_path / ("x" * 120) / "backend"
    key = PythonEnvs(tmp_path)._key(deep)
    assert key.startswith("backend-") and len(key) < 32


# --------------------------------------------------------------- the throwaway database

class _Docker:
    """Scripted ``docker`` CLI: records argv, answers like the real one."""

    def __init__(self, available: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.available = available

    def __call__(self, argv, cwd, timeout=None, input_text=None):
        self.calls.append(list(argv))
        sub = argv[1]
        if sub == "version":
            return RunResult("29.0.0" if self.available else "", "" if self.available else "Cannot connect to the Docker daemon", 0 if self.available else 1)
        if sub == "ps":
            return RunResult("stale1\n", "", 0)
        if sub == "run":
            return RunResult("abc123\n", "", 0)
        if sub == "port":
            return RunResult("127.0.0.1:49170\n", "", 0)
        return RunResult("", "", 0)


def test_the_database_starts_on_a_random_port_with_a_random_password():
    docker = _Docker()
    db = ThrowawayPostgres("ws", runner=docker)
    assert db.start() and db.port == 49170
    run = next(c for c in docker.calls if c[1] == "run")
    assert "127.0.0.1::5432" in run and f"{db.LABEL}=ws" in " ".join(run)
    assert ["docker", "rm", "-f", "stale1"] in docker.calls                     # a crashed run's container
    assert db.url("postgresql+asyncpg") == f"postgresql+asyncpg://app:{db.password}@127.0.0.1:49170/app"
    assert len(db.password) >= 20
    db.stop()
    assert ["docker", "rm", "-f", "abc123"] in docker.calls and db.container is None


def test_no_docker_means_no_database_and_a_reason():
    db = ThrowawayPostgres("ws", runner=_Docker(available=False))
    assert not db.start() and db.error == "Docker is not available"


def test_database_variables_point_at_the_container_in_the_projects_own_scheme():
    db = ThrowawayPostgres("ws", runner=_Docker())
    db.start()
    env = database_env(db, {"DATABASE_URL": "postgresql+asyncpg://x:y@localhost/z", "DB_HOST": "localhost",
                            "DB_PORT": "5432", "DB_PASSWORD": "x", "DB_NAME": "z", "REDIS_URL": "redis://r"})
    assert env["DATABASE_URL"].startswith("postgresql+asyncpg://app:")
    assert env["DB_HOST"] == "127.0.0.1" and env["DB_PORT"] == "49170" and env["DB_NAME"] == "app"
    assert env["DB_PASSWORD"] == db.password and "REDIS_URL" not in env


@pytest.mark.integration
def test_a_real_throwaway_database_round_trip():
    if subprocess.run(["docker", "version"], capture_output=True).returncode != 0:
        pytest.skip("no Docker daemon")
    db = ThrowawayPostgres("pytest-roundtrip")
    try:
        assert db.start(), db.error
        res = db.psql("CREATE TABLE t (id int); INSERT INTO t VALUES (1); SELECT count(*) FROM t;")
        assert res.ok and "1" in res.stdout
        assert db.reset().ok
    finally:
        db.stop()


# --------------------------------------------------------------- the boot check, for real

APP = '''from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}
'''


def _project(tmp_path: Path, main: str) -> LocalDiskExecutor:
    be = tmp_path / "proj" / "backend"
    (be / "app").mkdir(parents=True)
    (be / "app" / "__init__.py").write_text("", encoding="utf-8")
    (be / "app" / "main.py").write_text(main, encoding="utf-8")
    (be / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
    ex = LocalDiskExecutor(tmp_path)
    ex._backend_python = lambda be_dir: (sys.executable, [])   # the tool's venv has fastapi + uvicorn
    return ex


def test_a_working_fastapi_app_boots(tmp_path: Path):
    pytest.importorskip("uvicorn")
    result = _project(tmp_path, APP).boot("proj")
    assert result.passed, result.stderr


def test_an_app_that_cannot_import_fails_with_its_own_traceback(tmp_path: Path):
    pytest.importorskip("uvicorn")
    result = _project(tmp_path, "from app.routers import auth\n" + APP).boot("proj")
    assert not result.passed and not result.inconclusive
    assert "No module named 'app.routers'" in result.stdout + result.stderr
    assert result.stderr.startswith("[boot[proj/backend]]")


def test_no_server_means_nothing_to_boot(tmp_path: Path):
    (tmp_path / "proj" / "frontend").mkdir(parents=True)
    (tmp_path / "proj" / "frontend" / "package.json").write_text(json.dumps({"dependencies": {"react": "^18"}}), encoding="utf-8")
    result = LocalDiskExecutor(tmp_path).boot("proj")
    assert result.passed and "no server" in result.stdout


# --------------------------------------------------------------- npm install cache

def test_npm_install_is_skipped_while_the_manifest_is_unchanged(tmp_path: Path, monkeypatch):
    fe = tmp_path / "proj" / "frontend"
    fe.mkdir(parents=True)
    (fe / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    ex = LocalDiskExecutor(tmp_path)
    calls = []

    def fake(cmd, cwd=".", timeout=None, env=None):
        calls.append(cmd)
        (fe / "node_modules").mkdir(exist_ok=True)
        return RunResult("", "", 0)

    monkeypatch.setattr(ex, "run_command", fake)
    assert ex._npm_install("proj/frontend").ok and len(calls) == 1
    assert ex._npm_install("proj/frontend").stdout.startswith("(dependencies unchanged")
    assert len(calls) == 1
    (fe / "package.json").write_text('{"name": "x", "dependencies": {"y": "1"}}', encoding="utf-8")
    ex._npm_install("proj/frontend")
    assert len(calls) == 2
