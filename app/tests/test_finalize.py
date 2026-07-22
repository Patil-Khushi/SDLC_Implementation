"""Unit tests for ``finalize_node`` (app/graph/nodes.py) — the debug/test loop's success-path
commit+push step. Drives the node directly (like test_feature_history.py drives commit_node),
not the full graph, so each executor-capability branch is isolated and cheap to assert on.

Before this node existed, Code Review/Refactoring/Debugging/Unit Testing all wrote directly to
the workspace without ever committing (commit_node runs BEFORE them) — finalize_node is the one
place that captures those changes, mirroring commit_node's own push-capability detection
(``hasattr(executor, "publish_sweep")`` for the live-publish path, plain ``git_commit`` otherwise).
"""

from __future__ import annotations

from app.graph.nodes import finalize_node
from app.integrations.executor import FakeExecutor, RunResult, set_executor


class _SweepExecutor(FakeExecutor):
    """FakeExecutor that also advertises publish_sweep and records/scripts its outcome."""

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


def _base_state(**overrides) -> dict:
    state = {"project_id": "p1", "run_id": "r1", "push_enabled": False, "git_remote": ""}
    state.update(overrides)
    return state


def test_finalize_plain_commit_marks_completed() -> None:
    ex = FakeExecutor()  # no publish_sweep -> falls to the plain git_commit branch
    set_executor(ex)
    try:
        out = finalize_node(_base_state())  # type: ignore[arg-type]
        assert out["workflow_status"] == "completed"
        assert len(ex.commits) == 1
        assert ex.commits[0][0] == "p1"
        assert "[finalize] committed" in out["generation_summary"]
    finally:
        set_executor(None)


def test_finalize_commit_exception_marks_commit_failed() -> None:
    ex = FakeExecutor()
    set_executor(ex)
    try:
        ex.git_commit = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))  # type: ignore[method-assign]
        out = finalize_node(_base_state())  # type: ignore[arg-type]
        assert out["workflow_status"] == "commit_failed"
        assert "commit FAILED" in out["generation_summary"]
    finally:
        set_executor(None)


def test_finalize_uses_publish_sweep_when_push_enabled_and_supported() -> None:
    ex = _SweepExecutor()
    set_executor(ex)
    try:
        state = _base_state(push_enabled=True, git_remote="owner/repo", git_token="tok")
        out = finalize_node(state)  # type: ignore[arg-type]
        assert out["workflow_status"] == "completed"
        assert ex.sweep_calls == [("p1", "tok")]
        assert ex.commits == []  # push path uses publish_sweep, NOT the plain git_commit branch
        assert "pushed to 'dev'" in out["generation_summary"]
    finally:
        set_executor(None)


def test_finalize_push_failure_marks_push_failed_not_completed() -> None:
    ex = _SweepExecutor(sweep_result=RunResult(stdout="", stderr="remote rejected", exit_code=1))
    set_executor(ex)
    try:
        state = _base_state(push_enabled=True, git_remote="owner/repo")
        out = finalize_node(state)  # type: ignore[arg-type]
        assert out["workflow_status"] == "push_failed"
        assert "PUSH FAILED" in out["generation_summary"]
    finally:
        set_executor(None)


def test_finalize_push_exception_marks_push_failed() -> None:
    ex = _SweepExecutor()
    ex._sweep_raises = ConnectionError("no route to host")
    set_executor(ex)
    try:
        state = _base_state(push_enabled=True, git_remote="owner/repo")
        out = finalize_node(state)  # type: ignore[arg-type]
        assert out["workflow_status"] == "push_failed"
        assert "push FAILED" in out["generation_summary"]
    finally:
        set_executor(None)


def test_finalize_ignores_publish_sweep_when_push_not_enabled() -> None:
    # executor supports publish_sweep, but push_enabled is False -> plain git_commit branch.
    ex = _SweepExecutor()
    set_executor(ex)
    try:
        out = finalize_node(_base_state(push_enabled=False, git_remote="owner/repo"))  # type: ignore[arg-type]
        assert out["workflow_status"] == "completed"
        assert ex.sweep_calls == []
        assert len(ex.commits) == 1
    finally:
        set_executor(None)
