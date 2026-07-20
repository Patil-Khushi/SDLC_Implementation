"""Branch-aware commit history in the graph path.

commit_node now produces a real per-feature history (scaffold on ``main``, one ``feat(<id>)``
commit per work item on ``dev``) whenever the executor supports it, and falls back to the single
run-level commit otherwise. These tests pin both: the graph wiring (which paths/messages reach
the executor) without real git, and the actual git branch structure via LocalDiskExecutor.
"""

from __future__ import annotations

import subprocess

import pytest

from app.graph.nodes import commit_node
from app.integrations.executor import CommitResult, FakeExecutor, set_executor
from app.models import WorkItem


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True)
        return True
    except FileNotFoundError:
        return False


class _RecordingExecutor(FakeExecutor):
    """FakeExecutor that also advertises commit_feature_history and records the call."""

    def __init__(self) -> None:
        super().__init__()
        self.feature_calls: list[tuple] = []

    def commit_feature_history(
        self, project_dir, *, scaffold_files, feature_commits,
        base_branch="main", feature_branch="dev", **_kw,
    ) -> CommitResult:
        self.feature_calls.append(
            (project_dir, list(scaffold_files), list(feature_commits), base_branch, feature_branch)
        )
        return CommitResult(committed=True, sha="featsha")


def test_commit_node_uses_feature_history_when_supported() -> None:
    ex = _RecordingExecutor()
    set_executor(ex)
    try:
        state = {
            "project_id": "app",
            "run_id": "r",
            "scaffold_files": ["package.json", "README.md"],
            "work_items": [
                WorkItem(id="backend-modules-orders", endpoints=["POST /orders"],
                         target_files=["src/modules/orders/orders.controller.js"]),
                WorkItem(id="frontend-pages-auth-loginpage", screens=["Customer Login"],
                         target_files=["src/pages/auth/LoginPage/LoginPage.jsx"]),
            ],
            "generated_code": ["app/package.json", "app/src/modules/orders/orders.controller.js"],
        }
        out = commit_node(state)  # type: ignore[arg-type]

        assert out["workflow_status"] == "completed"
        assert len(ex.feature_calls) == 1
        project_dir, scaffold, feats, base, feat_branch = ex.feature_calls[0]
        assert (base, feat_branch) == ("main", "dev")
        assert scaffold == ["package.json", "README.md"]
        assert feats[0][0].startswith("feat(backend-modules-orders):")
        assert feats[0][1] == ["src/modules/orders/orders.controller.js"]
        assert feats[1][0].startswith("feat(frontend-pages-auth-loginpage):")
        assert ex.commits == []  # the single run-level git_commit path was NOT used
    finally:
        set_executor(None)


def test_commit_node_groups_work_items_by_feature() -> None:
    """Rule 6: items sharing a feature_id collapse into ONE feat(<feature>) commit; an untagged
    item still commits on its own."""
    ex = _RecordingExecutor()
    set_executor(ex)
    try:
        state = {
            "project_id": "app", "run_id": "r",
            "scaffold_files": ["package.json"],
            "work_items": [
                # feature 4.1 spans a backend module + a frontend page → ONE commit
                WorkItem(id="backend-modules-auth", feature_id="4.1", feature_title="Authentication",
                         endpoints=["POST /auth/login"], target_files=["src/modules/auth/auth.controller.js"]),
                WorkItem(id="frontend-pages-auth", feature_id="4.1", feature_title="Authentication",
                         screens=["Login"], target_files=["src/pages/auth/LoginPage.jsx"]),
                # untagged cross-cutting item → its own commit, per-item message
                WorkItem(id="backend-config", target_files=["src/config/env.js"]),
            ],
            "generated_code": [],
        }
        commit_node(state)  # type: ignore[arg-type]

        feats = ex.feature_calls[0][2]
        assert len(feats) == 2  # feature 4.1 (2 items merged) + the untagged config item
        assert feats[0] == (
            "feat(4.1): Authentication",
            ["src/modules/auth/auth.controller.js", "src/pages/auth/LoginPage.jsx"],
        )
        assert feats[1][0] == "feat(backend-config): 1 file(s)"  # untagged → per-item message
        assert feats[1][1] == ["src/config/env.js"]
    finally:
        set_executor(None)


def test_commit_node_falls_back_to_single_commit_without_support() -> None:
    ex = FakeExecutor()  # no commit_feature_history
    set_executor(ex)
    try:
        state = {
            "project_id": "app", "run_id": "r",
            "work_items": [WorkItem(id="x", target_files=["a.py"])],
            "generated_code": ["app/a.py"],
        }
        out = commit_node(state)  # type: ignore[arg-type]
        assert out["workflow_status"] == "completed"
        assert len(ex.commits) == 1  # single run-level commit (legacy behavior preserved)
    finally:
        set_executor(None)


@pytest.mark.skipif(not _git_available(), reason="git not on PATH")
def test_local_disk_executor_builds_main_and_dev(tmp_path) -> None:
    from scripts.local_executor import LocalDiskExecutor

    ex = LocalDiskExecutor(tmp_path)
    project = "app"
    ex.write_file(f"{project}/package.json", "{}\n")
    ex.write_file(f"{project}/README.md", "# app\n")
    ex.write_file(f"{project}/src/modules/orders/orders.controller.js", "// orders\n")
    ex.write_file(f"{project}/src/modules/orders/orders.service.js", "// orders svc\n")
    ex.write_file(f"{project}/src/modules/auth/auth.controller.js", "// auth\n")

    res = ex.commit_feature_history(
        project,
        scaffold_files=["package.json", "README.md"],
        feature_commits=[
            ("feat(backend-modules-orders): POST /orders",
             ["src/modules/orders/orders.controller.js", "src/modules/orders/orders.service.js"]),
            ("feat(backend-modules-auth): POST /auth/login",
             ["src/modules/auth/auth.controller.js"]),
        ],
    )
    assert res.committed

    repo = tmp_path / project

    def git(*a: str) -> str:
        return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True).stdout

    branches = git("branch", "--format=%(refname:short)").split()
    assert "main" in branches and "dev" in branches

    # main = scaffold ONLY (no src/ files)
    main_files = git("ls-tree", "-r", "--name-only", "main").split()
    assert "package.json" in main_files and "README.md" in main_files
    assert not any(f.startswith("src/") for f in main_files), main_files

    # dev = scaffold + every feature file
    dev_files = git("ls-tree", "-r", "--name-only", "dev").split()
    assert "src/modules/orders/orders.controller.js" in dev_files
    assert "src/modules/auth/auth.controller.js" in dev_files

    # dev history has the scaffold commit + one commit per feature
    log = git("log", "dev", "--format=%s")
    assert "chore: initial project scaffold" in log
    assert "feat(backend-modules-orders): POST /orders" in log
    assert "feat(backend-modules-auth): POST /auth/login" in log


@pytest.mark.skipif(not _git_available(), reason="git not on PATH")
def test_commit_feature_history_pushes_main_and_dev_to_remote(tmp_path) -> None:
    """Rules 4 & 8: with push+remote set, main and dev are pushed to a real remote (a local bare
    repo stands in for GitHub so no network/gh is needed)."""
    from scripts.local_executor import LocalDiskExecutor

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True)

    ex = LocalDiskExecutor(tmp_path / "work")
    project = "app"
    ex.write_file(f"{project}/package.json", "{}\n")
    ex.write_file(f"{project}/src/modules/orders/orders.controller.js", "// orders\n")
    ex.write_file(f"{project}/src/modules/auth/auth.controller.js", "// auth\n")

    res = ex.commit_feature_history(
        project,
        scaffold_files=["package.json"],
        feature_commits=[
            ("feat(backend-modules-orders): POST /orders", ["src/modules/orders/orders.controller.js"]),
            ("feat(backend-modules-auth): POST /auth/login", ["src/modules/auth/auth.controller.js"]),
        ],
        push=True,
        remote=str(bare),
    )
    assert res.exit_code == 0, res.stderr

    # inspect the REMOTE (bare) repo — both branches landed there
    def rgit(*a: str) -> str:
        return subprocess.run(["git", *a], cwd=bare, capture_output=True, text=True).stdout

    branches = rgit("branch", "--format=%(refname:short)").split()
    assert "main" in branches and "dev" in branches
    # main on the remote = scaffold only
    main_files = rgit("ls-tree", "-r", "--name-only", "main").split()
    assert "package.json" in main_files and not any(f.startswith("src/") for f in main_files)
    # dev on the remote carries the feature commits
    dev_log = rgit("log", "dev", "--format=%s")
    assert "feat(backend-modules-orders): POST /orders" in dev_log
    assert "feat(backend-modules-auth): POST /auth/login" in dev_log


@pytest.mark.skipif(not _git_available(), reason="git not on PATH")
def test_commit_feature_history_stops_on_push_failure(tmp_path) -> None:
    """Rule 8: a failed push stops the run (exit_code != 0) instead of silently continuing."""
    from scripts.local_executor import LocalDiskExecutor

    ex = LocalDiskExecutor(tmp_path / "work")
    project = "app"
    ex.write_file(f"{project}/package.json", "{}\n")
    ex.write_file(f"{project}/src/a.js", "// a\n")

    res = ex.commit_feature_history(
        project,
        scaffold_files=["package.json"],
        feature_commits=[("feat(x): a", ["src/a.js"])],
        push=True,
        remote=str(tmp_path / "nonexistent"),  # not a repo → push fails
    )
    assert res.exit_code != 0
    assert "PUSH FAILED" in res.stdout
