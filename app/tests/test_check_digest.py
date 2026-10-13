"""check_digest: turning failed-check output into counts, per-file errors and environment signals.

The shapes below are the ones the real tools print (non-interactive tsc prints NO "Found N errors"
summary, pytest -q prints "N failed, M passed in Xs", pip prints the MSVC message on a missing
wheel) — the case that motivated the module was a real run where tsc's 127 error lines and pip's
asyncpg build failure were reduced to "2 failing checks".
"""

from __future__ import annotations

from app.services import check_digest as cd

TSC = (
    "[tsc[proj/frontend]] src/pages/Login.tsx(12,5): error TS2322: Type '\"ghost\"' is not assignable.\n"
    "src/pages/Login.tsx(30,1): error TS2304: Cannot find name 'foo'.\n"
    "src/hooks/useAuth.ts(3,20): error TS2307: Cannot find module '@/store/authStore'.\n"
)
PIP = (
    "[pip[proj/backend]] Building wheel for asyncpg (pyproject.toml): finished with status 'error'\n"
    "error: Microsoft Visual C++ 14.0 or greater is required.\n"
    "ERROR: Failed building wheel for asyncpg\n"
)


def test_split_tool_blocks_recovers_label_and_directory() -> None:
    blocks = cd.split_tool_blocks(TSC + "\n" + PIP)
    assert [(b.label, b.directory) for b in blocks] == [("tsc", "proj/frontend"), ("pip", "proj/backend")]


def test_unlabelled_output_is_one_block() -> None:
    blocks = cd.split_tool_blocks("Tests: 5 failed, 1 passed")
    assert len(blocks) == 1 and blocks[0].label == ""


def test_errors_by_file_maps_tool_relative_paths_to_project_relative() -> None:
    counts = cd.errors_by_file(TSC, project_dir="proj")
    assert counts == {"frontend/src/pages/Login.tsx": 2, "frontend/src/hooks/useAuth.ts": 1}


def test_errors_by_file_reads_pytest_compileall_and_traceback_shapes() -> None:
    text = (
        "[pytest[proj/backend]] FAILED tests/test_auth.py::test_login - AssertionError\n"
        "ERROR tests/test_db.py - ModuleNotFoundError\n"
        "[py] *** Error compiling './proj/backend/app/main.py'...\n"
        '  File "C:\\work\\proj\\backend\\app\\config.py", line 9, in <module>\n'
        '  File "C:\\venv\\Lib\\site-packages\\fastapi\\routing.py", line 1, in x\n'
    )
    counts = cd.errors_by_file(text, project_dir="proj")
    assert counts["backend/tests/test_auth.py"] == 1
    assert counts["backend/tests/test_db.py"] == 1
    assert counts["backend/app/main.py"] == 1
    assert counts["backend/app/config.py"] == 1
    assert not any("site-packages" in p or "fastapi" in p for p in counts)   # third-party frames dropped


def test_error_count_prefers_the_tools_own_summary() -> None:
    assert cd.error_count("Found 209 errors in 45 files.") == 209
    assert cd.error_count("Found 1 error in src/a.ts:3") == 1
    assert cd.error_count("Found 3 errors.\n...\nFound 4 errors.") == 7            # two frontends: summed
    assert cd.error_count("Tests: 341 failed, 4046 passed, 4387 total") == 341
    assert cd.error_count("==== 3 failed, 10 passed, 2 errors in 0.52s ====") == 5
    assert cd.error_count("2 errors in 0.10s") == 2
    assert cd.error_count("nothing countable here") is None


def test_digest_counts_tsc_error_lines_when_there_is_no_summary_and_pip_as_one() -> None:
    checks = [
        {"name": "compile", "passed": False, "stderr": TSC, "stdout": ""},
        {"name": "build", "passed": False, "stderr": PIP, "stdout": ""},
        {"name": "lint", "passed": True, "stderr": "", "stdout": ""},
    ]
    digest = cd.digest_checks(checks, project_dir="proj")
    assert digest.error_count == 3 + 1          # three tsc error lines + one silent pip failure
    assert digest.quantified is True
    assert digest.top_files(1) == [("frontend/src/pages/Login.tsx", 2)]


def test_environment_signals_name_the_unfixable_causes() -> None:
    signals = cd.environment_signals(PIP + "\n[npm-install[proj/frontend]] [timed out]\nNameResolutionError: pypi.org")
    assert any("C compiler" in s for s in signals)
    assert "pip could not build asyncpg from source" in signals
    assert any("timed out" in s for s in signals)
    assert any("network" in s for s in signals)
    assert cd.environment_signals("TypeError: x is not a function") == []


def test_check_output_drops_stdout_already_carried_by_stderr() -> None:
    same = {"stderr": "[tsc] boom\nmore", "stdout": "boom\nmore"}
    assert cd.check_output(same) == "[tsc] boom\nmore"
    different = {"stderr": "warn", "stdout": "Tests: 2 failed"}
    assert cd.check_output(different) == "warn\nTests: 2 failed"
    assert cd.check_output({"stderr": "", "stdout": "only out"}) == "only out"
