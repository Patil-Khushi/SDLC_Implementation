"""Unit tests for ``debug_publish_node`` (app/graph/nodes.py) — the FIXED commit+push step on the
Debugging<->Unit-Test loop's pass edge. Drives the node directly (like test_feature_history.py
drives commit_node), so each executor-capability branch is isolated.

The node is the debug/test analogue of ``refactoring_publish_node``: it persists the loop's output
(debug fixes + generated unit tests) to 'dev' so Security's re-scan and finalize's PR carry them.
It is a MID-pipeline publish step, NOT the terminal — so, unlike a terminal node, it never stamps
``workflow_status`` and a push/commit failure is non-fatal (logged + noted, run continues).
"""

from __future__ import annotations

from app.graph.nodes import debug_publish_node
from app.integrations.executor import FakeExecutor, RunResult, set_executor


class _SweepExecutor(FakeExecutor):
    """FakeExecutor that also advertises publish_sweep (the local-disk push capability) and
    records/scripts its outcome — FakeExecutor itself has no publish_sweep, so the plain
    git_commit branch is exercised by the base class."""

    def __init__(self, *, sweep_result: RunResult | None = None) -> None:
        super().__init__()
        self.sweep_calls: list[tuple] = []
        self._sweep_result = sweep_result or RunResult(stdout="pushed", stderr="", exit_code=0)
        self._sweep_raises: Exception | None = None

    def publish_sweep(self, project_dir, *, token=None) -> RunResult:
        self.sweep_calls.append((project_dir, token))
        if self._sweep_raises:
            raise self._sweep_raises
        return self._sweep_result


def _state(**overrides) -> dict:
    # A run where the loop produced tests (so the node is not a no-op) and push is off by default.
    state = {
        "project_id": "p1", "run_id": "r1",
        "push_enabled": False, "git_remote": "",
        "unit_tests": ["p1/tests/test_x.py"],
    }
    state.update(overrides)
    return state


def test_noop_when_loop_produced_nothing() -> None:
    # No tests generated AND the debug agent never ran -> nothing to persist -> pure pass-through.
    ex = FakeExecutor()
    set_executor(ex)
    try:
        out = debug_publish_node(_state(unit_tests=[], debug_attempt=0))  # type: ignore[arg-type]
        assert ex.commits == []                       # no commit at all
        assert "workflow_status" not in out           # never stamps a terminal status
        assert "[publish]" not in (out.get("generation_summary") or "")
    finally:
        set_executor(None)


def test_acts_when_only_debug_ran_even_without_tests() -> None:
    # Debug agent ran (debug_attempt>0) but no unit tests on record -> still persist the fixes.
    ex = FakeExecutor()
    set_executor(ex)
    try:
        out = debug_publish_node(_state(unit_tests=[], debug_attempt=2))  # type: ignore[arg-type]
        assert len(ex.commits) == 1
        assert "[publish]" in out["generation_summary"]
    finally:
        set_executor(None)


def test_plain_commit_when_push_disabled() -> None:
    ex = FakeExecutor()  # no publish_sweep -> plain git_commit branch
    set_executor(ex)
    try:
        out = debug_publish_node(_state())  # type: ignore[arg-type]
        assert len(ex.commits) == 1
        assert ex.commits[0] == ("p1", "test(r1): debug fixes + unit tests")
        assert "committed locally" in out["generation_summary"]
        assert "workflow_status" not in out           # mid-pipeline: never terminal
    finally:
        set_executor(None)


def test_uses_publish_sweep_when_push_enabled_and_supported() -> None:
    ex = _SweepExecutor()
    set_executor(ex)
    try:
        out = debug_publish_node(_state(push_enabled=True, git_remote="owner/repo", git_token="tok"))  # type: ignore[arg-type]
        assert ex.sweep_calls == [("p1", "tok")]
        assert ex.commits == []                       # push path used publish_sweep, NOT git_commit
        assert "pushed to 'dev'" in out["generation_summary"]
        assert "workflow_status" not in out
    finally:
        set_executor(None)


def test_ignores_publish_sweep_when_push_not_enabled() -> None:
    # Executor supports publish_sweep, but push disabled -> falls to the plain git_commit branch.
    ex = _SweepExecutor()
    set_executor(ex)
    try:
        debug_publish_node(_state(push_enabled=False, git_remote="owner/repo"))  # type: ignore[arg-type]
        assert ex.sweep_calls == []
        assert len(ex.commits) == 1
    finally:
        set_executor(None)


def test_push_failure_is_non_fatal_and_noted() -> None:
    ex = _SweepExecutor(sweep_result=RunResult(stdout="", stderr="remote rejected", exit_code=1))
    set_executor(ex)
    try:
        out = debug_publish_node(_state(push_enabled=True, git_remote="owner/repo"))  # type: ignore[arg-type]
        assert "PUSH FAILED" in out["generation_summary"]
        assert "workflow_status" not in out           # non-fatal: no terminal error status, run continues
    finally:
        set_executor(None)


def test_push_exception_is_non_fatal_and_noted() -> None:
    ex = _SweepExecutor()
    ex._sweep_raises = ConnectionError("no route to host")
    set_executor(ex)
    try:
        out = debug_publish_node(_state(push_enabled=True, git_remote="owner/repo"))  # type: ignore[arg-type]
        assert "FAILED" in out["generation_summary"]
        assert "workflow_status" not in out
    finally:
        set_executor(None)


def test_commit_exception_is_non_fatal_and_noted() -> None:
    ex = FakeExecutor()
    set_executor(ex)
    try:
        ex.git_commit = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))  # type: ignore[method-assign]
        out = debug_publish_node(_state())  # type: ignore[arg-type]
        assert "FAILED" in out["generation_summary"]
        assert "workflow_status" not in out
    finally:
        set_executor(None)
