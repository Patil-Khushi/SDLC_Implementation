"""Smoke check + repair (Track B, B3) in ``scripts/feature_commit.py``.

The code-gen gate is completeness-only, so nothing ever verified that generated files actually
work together — that's why repos shipped "never executed even once". smoke_check() runs cheap
static checks (py_compile + pyflakes for Python; ``node --check`` for JS) over a feature's files,
and repair_smoke_errors() regenerates the ones that fail. These tests pin: syntax detection, the
undefined-name detection that catches the naming-mismatch class, honest skipping of files that need
a bundler, that it never raises, and that the repair loop fixes a flagged file. All offline (the
repair test uses a scripted gateway; the pyflakes/node tests skip if the tool isn't present).
"""

from __future__ import annotations

import importlib.util
import shutil
from collections import deque
from pathlib import Path

import scripts.feature_commit as fc

_HAS_PYFLAKES = importlib.util.find_spec("pyflakes") is not None
_HAS_NODE = shutil.which("node") is not None


class _ScriptedGateway:
    """Serves canned ``.complete()`` replies in order; records prompts for inspection."""

    def __init__(self, responses: list[str]) -> None:
        self._queue: deque[str] = deque(responses)
        self.calls: list[str] = []

    def complete(self, prompt: str, *, system: str | None = None, max_tokens: int | None = None) -> str:
        self.calls.append(prompt)
        return self._queue.popleft() if self._queue else '{"files":[]}'


# ------------------------------------------------------------------- detection

def test_clean_python_passes(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("import os\n\n\ndef f():\n    return os.getcwd()\n", encoding="utf-8")
    result = fc.smoke_check(tmp_path, ["ok.py"])
    assert result.ok
    assert not result.errors_by_file


def test_python_syntax_error_is_caught(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    result = fc.smoke_check(tmp_path, ["bad.py"])
    assert not result.ok
    assert "bad.py" in result.errors_by_file
    assert any("SyntaxError" in m for m in result.errors_by_file["bad.py"])


def test_undefined_name_is_caught_by_pyflakes(tmp_path: Path) -> None:
    if not _HAS_PYFLAKES:
        import pytest
        pytest.skip("pyflakes not installed")
    # Uses `UserSchema` without importing/defining it — the exact cross-file naming-mismatch class.
    (tmp_path / "router.py").write_text("def handler():\n    return UserSchema()\n", encoding="utf-8")
    result = fc.smoke_check(tmp_path, ["router.py"])
    assert not result.ok
    assert any("undefined name" in m and "UserSchema" in m for m in result.errors_by_file["router.py"])


def test_pyflakes_absence_is_reported_not_fatal(tmp_path: Path, monkeypatch) -> None:
    # Force the "pyflakes missing" branch and confirm py_compile still runs and nothing crashes.
    monkeypatch.setattr(fc.importlib.util, "find_spec", lambda name: None)
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    result = fc.smoke_check(tmp_path, ["ok.py"])
    assert result.ok
    assert any("pyflakes not installed" in s for s in result.skipped)
    assert any("py_compile" in c for c in result.checked)


# ------------------------------------------------------------------- node (skips if absent)

def test_js_syntax_error_is_caught(tmp_path: Path) -> None:
    if not _HAS_NODE:
        import pytest
        pytest.skip("node not on PATH")
    (tmp_path / "server.js").write_text("const x = ;\n", encoding="utf-8")
    result = fc.smoke_check(tmp_path, ["server.js"])
    assert not result.ok
    assert "server.js" in result.errors_by_file


# ------------------------------------------------------------------- honest skipping

def test_tsx_is_skipped_not_passed(tmp_path: Path) -> None:
    # A .tsx file cannot be checked without a bundler; it must be reported as skipped, and because
    # it is the ONLY file, there is nothing to check -> ok stays True but a skip note is present.
    (tmp_path / "App.tsx").write_text("export const A = () => <div/>;\n", encoding="utf-8")
    result = fc.smoke_check(tmp_path, ["App.tsx"])
    assert result.ok
    assert any("tsx" in s or "tsc" in s for s in result.skipped)


def test_missing_files_are_ignored(tmp_path: Path) -> None:
    result = fc.smoke_check(tmp_path, ["does/not/exist.py"])
    assert result.ok  # nothing to check, no crash


def test_smoke_never_raises_on_unreadable_input(tmp_path: Path) -> None:
    # Passing junk instead of a list must be swallowed, not raised.
    result = fc.smoke_check(tmp_path, None)  # type: ignore[arg-type]
    assert isinstance(result, fc.SmokeResult)
    assert any("aborted" in s for s in result.skipped)


# ------------------------------------------------------------------- repair loop

def test_repair_fixes_a_flagged_file(tmp_path: Path) -> None:
    broken = tmp_path / "svc.py"
    broken.write_text("def f(:\n    pass\n", encoding="utf-8")  # syntax error
    result = fc.smoke_check(tmp_path, ["svc.py"])
    assert not result.ok

    fixed_json = '{"files":[{"path":"svc.py","content":"def f():\\n    return 1\\n"}]}'
    gw = _ScriptedGateway([fixed_json])
    current: dict[str, str] = {}

    final, repaired = fc.repair_smoke_errors(
        gw, tmp_path, "CONTRACT", ["svc.py"], result, current, max_rounds=1,
    )

    assert repaired == ["svc.py"]
    assert final.ok
    assert broken.read_text(encoding="utf-8") == "def f():\n    return 1\n"
    # The repair prompt carried the contract and the actual error.
    assert "CONTRACT" in gw.calls[0]
    assert "SyntaxError" in gw.calls[0]


def test_repair_leaves_file_flagged_when_llm_cannot_fix(tmp_path: Path) -> None:
    broken = tmp_path / "svc.py"
    broken.write_text("def f(:\n    pass\n", encoding="utf-8")
    result = fc.smoke_check(tmp_path, ["svc.py"])

    # LLM returns a still-broken file -> stays flagged, no crash, bounded by max_rounds.
    still_broken = '{"files":[{"path":"svc.py","content":"def g(:\\n    pass\\n"}]}'
    gw = _ScriptedGateway([still_broken, still_broken])

    final, _repaired = fc.repair_smoke_errors(
        gw, tmp_path, "", ["svc.py"], result, {}, max_rounds=1,
    )
    assert not final.ok
    assert "svc.py" in final.errors_by_file
