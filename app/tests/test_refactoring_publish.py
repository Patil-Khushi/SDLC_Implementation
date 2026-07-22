"""refactoring_publish_node: FIXED commit + push of the refactoring agent's edits to 'dev'.

The node is deterministic (never LLM-formed — rule 2) and mirrors feature_publish_node /
commit_node: with push enabled and a publish-capable executor it commits exactly the edited
paths on the working branch and pushes; on the sandbox/test path it falls back to a plain
fixed-path ``git_commit``; when refactoring edited nothing it passes straight through (no
commit at all — the graph acceptance tests' "committed exactly once" invariant holds).
"""

from typing import Any

from app.graph.nodes import refactoring_publish_node
from app.integrations.executor import CommitResult, FakeExecutor, RunResult, set_executor


class _PublishingExecutor(FakeExecutor):
    """FakeExecutor + the local-disk executor's incremental publish capability, recorded."""

    def __init__(self, *, publish_exit_code: int = 0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._publish_exit_code = publish_exit_code
        self.published: list[dict[str, Any]] = []

    def publish_feature(self, project_dir, message, paths, *,
                        feature_branch="dev", base_branch="main", token=None) -> RunResult:
        self.published.append({
            "project_dir": str(project_dir), "message": message, "paths": list(paths),
            "feature_branch": feature_branch, "token": token,
        })
        return RunResult(stdout="", stderr="", exit_code=self._publish_exit_code)


def _state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": "r1", "project_id": "proj",
        "refactored_files": ["proj/src/foo.py", "proj/src/bar.py"],
        "generation_summary": "",
    }
    base.update(over)
    return base


def _run(executor, state) -> dict[str, Any]:
    set_executor(executor)
    try:
        return refactoring_publish_node(state)
    finally:
        set_executor(None)


def test_noop_when_nothing_was_refactored() -> None:
    executor = _PublishingExecutor()
    state = _run(executor, _state(refactored_files=[], push_enabled=True, git_remote="me/app"))

    assert executor.published == []
    assert executor.commits == []                       # not even a local commit
    assert state["generation_summary"] == ""


def test_noop_when_refactored_files_key_absent() -> None:
    executor = _PublishingExecutor()
    state = _run(executor, {"run_id": "r1", "project_id": "proj"})  # key never set (early escalate)

    assert executor.published == []
    assert executor.commits == []
    assert "generation_summary" not in state


def test_push_enabled_publishes_edited_paths_to_dev() -> None:
    executor = _PublishingExecutor()
    state = _run(executor, _state(push_enabled=True, git_remote="me/app", git_token="tok"))

    assert len(executor.published) == 1
    pub = executor.published[0]
    assert pub["project_dir"] == "proj"
    assert pub["paths"] == ["src/foo.py", "src/bar.py"]   # project prefix stripped for staging
    assert pub["feature_branch"] == "dev"                 # settings.working_branch default
    assert pub["token"] == "tok"
    assert pub["message"].startswith("refactor(r1): ")
    assert executor.commits == []                         # publish path, not the plain-commit path
    assert "pushed to 'dev'" in state["generation_summary"]


def test_branch_state_overrides_the_working_branch() -> None:
    executor = _PublishingExecutor()
    _run(executor, _state(push_enabled=True, git_remote="me/app", branch="hotfix"))

    assert executor.published[0]["feature_branch"] == "hotfix"


def test_push_disabled_falls_back_to_fixed_path_commit() -> None:
    # Sandbox/test path: no publish; a plain git_commit records the refactor in the workspace repo.
    executor = _PublishingExecutor()
    state = _run(executor, _state())  # push_enabled unset

    assert executor.published == []
    assert executor.commits == [("proj", "refactor(r1): apply code review fixes to 2 file(s)")]
    assert "committed locally" in state["generation_summary"]


def test_failed_local_commit_is_noted_not_reported_as_success() -> None:
    # git_commit does not raise on failure — it returns CommitResult(committed=False, ...). The
    # fallback branch must inspect that result, not blindly report "committed locally".
    class _FailingCommitExecutor(FakeExecutor):
        def git_commit(self, project_dir, message) -> CommitResult:
            self.commits.append((str(project_dir), message))
            return CommitResult(committed=False, stderr="fatal: bad identity", exit_code=1)

    executor = _FailingCommitExecutor()
    state = _run(executor, _state())  # push disabled -> fixed-path git_commit fallback

    assert "(COMMIT FAILED)" in state["generation_summary"]


def test_exception_on_fallback_commit_is_labeled_local_commit_not_push() -> None:
    # The single except block must distinguish which action failed — a local-commit exception on
    # the non-push fallback path must not be mislabeled "push FAILED" (misdirects triage).
    class _ExplodingCommitExecutor(FakeExecutor):
        def git_commit(self, project_dir, message) -> CommitResult:
            raise RuntimeError("workspace repo missing")

    executor = _ExplodingCommitExecutor()
    state = _run(executor, _state())  # push disabled -> fixed-path git_commit fallback

    assert "refactoring local commit FAILED: workspace repo missing" in state["generation_summary"]
    assert "push FAILED" not in state["generation_summary"]


def test_executor_without_publish_capability_commits_even_when_push_enabled() -> None:
    executor = FakeExecutor()  # no publish_feature at all (the MCP/sandbox shape)
    state = _run(executor, _state(push_enabled=True, git_remote="me/app"))

    assert len(executor.commits) == 1
    assert "committed locally" in state["generation_summary"]


def test_failed_push_is_noted_not_raised() -> None:
    executor = _PublishingExecutor(publish_exit_code=1)
    state = _run(executor, _state(push_enabled=True, git_remote="me/app"))

    assert "PUSH FAILED" in state["generation_summary"]   # noted for the run summary


def test_publish_exception_never_crashes_the_run() -> None:
    class _ExplodingExecutor(_PublishingExecutor):
        def publish_feature(self, *a: Any, **k: Any) -> RunResult:
            raise RuntimeError("remote unreachable")

    executor = _ExplodingExecutor()
    state = _run(executor, _state(push_enabled=True, git_remote="me/app"))

    assert "refactoring push FAILED: remote unreachable" in state["generation_summary"]
