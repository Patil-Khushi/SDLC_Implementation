"""Finding and running the target repository's OWN test suite.

``Executor.test()`` gates pytest on ``requirements.txt`` and npm on ``package.json``. A repo
packaged with ``pyproject.toml`` — most Python projects since ~2021 — matches neither, so it
returns ``passed=True`` having executed nothing. That vacuous green is what these tests exist to
prevent: it makes a run claim the change was verified when no test was run at all.

The three-way outcome is the heart of it. ``inconclusive`` ("the suite could not run") must never
collapse into ``failed`` ("your change broke it"): a repo whose dev dependencies are missing would
otherwise block a correct change and send the model off fixing code that was never broken.
"""

from __future__ import annotations

import json

from app.integrations.executor import FakeExecutor, RunResult
from app.services.repo_inventory import inventory
from app.services.test_command import TestCommand, detect, run


def _inv(ex: FakeExecutor) -> dict:
    return inventory(ex, "p1").as_dict()


def _detect(files: dict[str, str]):
    ex = FakeExecutor(files={f"p1/{k}": v for k, v in files.items()})
    return detect(ex, "p1", _inv(ex)), ex


# --- detection ------------------------------------------------------------------------------------


def test_a_pyproject_python_repo_is_detected() -> None:
    # THE case the old check missed entirely: no requirements.txt, so Executor.test() ran nothing
    # and reported success.
    cmd, _ = _detect({
        "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        "src/pkg/__init__.py": "", "tests/test_pkg.py": "def test_x(): pass\n",
    })
    assert cmd is not None
    assert cmd.label == "pytest"
    assert "pyproject.toml" in cmd.evidence


def test_a_src_layout_gets_pythonpath_so_the_package_is_importable() -> None:
    # An uninstalled src/ package is the most common reason a suite looks broken when it is merely
    # un-importable — which would otherwise be misread as a regression.
    cmd, _ = _detect({
        "pyproject.toml": "[tool.pytest.ini_options]\n",
        "src/pkg/__init__.py": "", "tests/test_pkg.py": "def test_x(): pass\n",
    })
    assert cmd.env.get("PYTHONPATH") == "src"


def test_a_flat_layout_needs_no_pythonpath() -> None:
    cmd, _ = _detect({"pytest.ini": "[pytest]\n", "tests/test_x.py": "def test_x(): pass\n"})
    assert "PYTHONPATH" not in cmd.env


def test_an_npm_test_script_wins() -> None:
    cmd, _ = _detect({
        "package.json": json.dumps({"scripts": {"test": "jest --ci"}}),
        "src/index.js": "module.exports = {}\n",
    })
    assert cmd.label == "npm test"
    assert cmd.argv[:2] == ["npm", "test"]


def test_npms_placeholder_script_is_not_a_test_suite() -> None:
    # `npm init` writes a script that just errors. Treating it as a suite would make every run
    # "fail its tests" for a reason that has nothing to do with the change.
    placeholder = 'echo "Error: no test specified" && exit 1'
    cmd, _ = _detect({
        "package.json": json.dumps({"scripts": {"test": placeholder}}),
        "src/index.js": "x\n",
    })
    assert cmd is None or cmd.label != "npm test"


def test_go_and_rust_repos_are_detected() -> None:
    go, _ = _detect({"go.mod": "module x\n", "main.go": "package main\n"})
    assert go.label == "go test"
    rust, _ = _detect({"Cargo.toml": "[package]\n", "src/main.rs": "fn main() {}\n"})
    assert rust.label == "cargo test"


def test_a_makefile_test_target_is_a_last_resort() -> None:
    cmd, _ = _detect({"Makefile": "build:\n\tcc x\n\ntest:\n\t./run-tests\n", "x.c": "int main(){}"})
    assert cmd.label == "make test"


def test_a_repo_with_no_suite_returns_none() -> None:
    # A legitimate outcome. The caller reports "not test-verified" rather than inventing a check.
    cmd, _ = _detect({"README.md": "# docs\n", "notes.txt": "hi\n"})
    assert cmd is None


# --- outcome classification -----------------------------------------------------------------------


class _ScriptedExecutor(FakeExecutor):
    def __init__(self, result: RunResult) -> None:
        super().__init__()
        self._result = result

    def run_command(self, cmd, cwd=".", timeout=None, env=None):  # type: ignore[override]
        self.commands.append(list(cmd))
        return self._result


_PYTEST = TestCommand(argv=["python", "-m", "pytest"], label="pytest", evidence="x")


def _outcome(**kw):
    return run(_ScriptedExecutor(RunResult(**{"stdout": "", "stderr": "", **kw})), "p1", _PYTEST)


def test_pytest_exit_0_is_a_pass_and_carries_its_own_summary() -> None:
    got = _outcome(stdout="69 passed in 0.15s\n", exit_code=0)
    assert got.status == "passed" and got.passed
    assert "69 passed" in got.summary


def test_pytest_exit_1_is_a_real_failure() -> None:
    got = _outcome(stdout="3 failed, 66 passed in 0.2s\n", exit_code=1)
    assert got.status == "failed" and got.conclusive
    assert "3 failed" in got.summary


def test_pytest_collecting_nothing_is_inconclusive_not_a_pass() -> None:
    # Exit 5. Reporting this as a pass is the vacuous green in a different disguise.
    got = _outcome(stdout="no tests ran\n", exit_code=5)
    assert got.status == "inconclusive" and not got.passed and not got.conclusive


def test_a_pytest_internal_error_is_inconclusive_not_a_failure() -> None:
    # Exit 3/4 mean the run did not happen properly. Blaming the change would be wrong.
    for code in (2, 3, 4):
        assert _outcome(stderr="INTERNALERROR\n", exit_code=code).status == "inconclusive"


def test_a_missing_dev_dependency_is_inconclusive() -> None:
    # THE most common brownfield case: the suite imports freezegun, it is not installed, the whole
    # collection errors. That is not a regression caused by the change.
    got = _outcome(stderr="ModuleNotFoundError: No module named 'freezegun'\n", exit_code=2)
    assert got.status == "inconclusive"


def test_a_missing_runner_is_inconclusive() -> None:
    assert _outcome(stderr="command not found: pytest", exit_code=127).status == "inconclusive"


def test_a_timeout_is_inconclusive() -> None:
    assert _outcome(stderr="[timed out]", exit_code=124, timed_out=True).status == "inconclusive"


def test_an_executor_that_raises_is_inconclusive_not_a_crash() -> None:
    class _Exploding(FakeExecutor):
        def run_command(self, *a, **kw):  # type: ignore[override]
            raise RuntimeError("boom")

    assert run(_Exploding(), "p1", _PYTEST).status == "inconclusive"


def test_a_non_pytest_runner_failing_to_start_is_inconclusive() -> None:
    npm = TestCommand(argv=["npm", "test"], label="npm test", evidence="x")
    ex = _ScriptedExecutor(RunResult(stdout="npm ERR! missing script: test", stderr="", exit_code=1))
    assert run(ex, "p1", npm).status == "inconclusive"


def test_the_command_env_extends_rather_than_replaces_the_environment() -> None:
    # env= REPLACES a subprocess environment; a bare dict would strip PATH and the interpreter
    # would not even be found. Same trap that broke the clone in M1.
    import os

    from app.services.test_command import _env_for

    env = _env_for(TestCommand(argv=["x"], label="l", evidence="e", env={"PYTHONPATH": "src"}))
    assert env["PYTHONPATH"] == "src"
    assert set(os.environ).issubset(set(env))
    assert _env_for(_PYTEST) is None          # no overrides -> inherit as-is


# --- two bugs caught on the first REAL run, not by the tests above -------------------------------


def test_pytest_runs_under_the_interpreter_that_actually_has_it() -> None:
    """A bare "python" resolves via PATH to whatever comes first.

    Observed live: it picked a base install with no pytest, `python -m pytest` exited 1 — the SAME
    code a real test failure uses — and the run reported "the tests failed" when nothing had run.
    """
    import sys

    cmd, _ = _detect({
        "pyproject.toml": "[tool.pytest.ini_options]\n",
        "tests/test_x.py": "def test_x(): pass\n",
    })
    assert cmd.argv[0] == sys.executable
    assert cmd.argv[0] != "python"


def test_a_missing_runner_exiting_1_is_inconclusive_not_a_failure() -> None:
    """`python -m pytest` with pytest absent exits 1, indistinguishable from a real failure by
    exit code alone. Calling it "failed" is exactly the collapse this module exists to prevent —
    it would block a correct change and send the model off fixing code that was never broken."""
    got = _outcome(
        stderr=r"C:\Users\x\AppData\Roaming\uv\python\cpython-3.14\python.exe: No module named pytest",
        exit_code=1,
    )
    assert got.status == "inconclusive"
    assert "not installed" in got.summary


def test_an_import_error_inside_a_test_is_still_a_real_failure() -> None:
    # The guard above must not swallow genuine failures: a test that itself raises ImportError has
    # run, and its failure is real. The pattern is anchored to an INTERPRETER's startup error.
    got = _outcome(
        stdout="tests/test_x.py:3: in <module>\n    import missing_thing\n"
               "E   ModuleNotFoundError: No module named 'missing_thing'\n"
               "1 failed in 0.1s\n",
        exit_code=1,
    )
    assert got.status == "failed"


def test_a_change_request_written_by_powershell_keeps_its_title(tmp_path) -> None:
    """Found by a real run. PowerShell's `Set-Content -Encoding utf8` writes a UTF-8 BOM; read as
    plain utf-8 the heading test fails and the title degrades to the filename, which then becomes
    the commit subject and the pull-request title (observed: `chore: cr`)."""
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "scripts"))
    from run_change_request import _load_change_request

    cr = tmp_path / "cr.md"
    cr.write_bytes("﻿# Reject an empty secret key\n\nThe body.\n".encode("utf-8"))
    got = _load_change_request(cr)

    assert got["title"] == "Reject an empty secret key"
    assert got["description"] == "The body."
    assert "﻿" not in got["title"] + got["description"]


def test_a_bom_prefixed_json_change_request_still_parses(tmp_path) -> None:
    import json as _json
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "scripts"))
    from run_change_request import _load_change_request

    cr = tmp_path / "cr.json"
    payload = {"id": "CR-9", "title": "Do the thing", "description": "Details."}
    cr.write_bytes("﻿".encode("utf-8") + _json.dumps(payload).encode("utf-8"))

    assert _load_change_request(cr)["title"] == "Do the thing"
