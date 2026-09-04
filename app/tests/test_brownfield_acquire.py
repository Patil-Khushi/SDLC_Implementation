"""Brownfield acquisition: clone an EXISTING repo, survey it, and touch nothing.

The safety property under test is narrow and absolute: **acquisition writes nothing to the target
repository.** Greenfield's first node cannot be reused to get here — ``scaffold_node``
unconditionally overwrites .gitignore, README.md, package.json/requirements.txt, Dockerfile,
docker-compose.yml and the jest/babel configs from ``DEFAULT_CAPABILITIES`` (never reading disk),
then calls ``publish_scaffold`` -> ``git checkout -B main`` + ``gh repo create`` + ``git push -u
origin main``. Against a clone that push is a clean FAST-FORWARD onto the user's default branch.
So the tests here assert the negative as hard as the positive: no scaffold write, no repo creation,
no push.

Everything runs on a seeded ``FakeExecutor`` — no network, no Docker, no LLM, well under a second.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.graph.nodes import acquire_repo_node
from app.graph.router import route_entry
from app.graph.state import new_state
from app.integrations.executor import FakeExecutor, RunResult, set_executor

_REPO_URL = "https://github.com/acme/widgets"
_HEAD = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"

#: An existing app, as it looks after `git clone` — the state acquisition actually meets.
_CLONED_FILES = {
    "p1/src/auth/login.py": "def login():\n    return True\n",
    "p1/src/api/routes.py": "ROUTES = []\n",
    "p1/tests/test_login.py": "def test_login(): pass\n",
    "p1/README.md": "# Widgets\n\nThe real project readme.\n",
    "p1/.gitignore": "*.pem\n.env.local\nterraform.tfstate\nsecrets/\n",
    "p1/package.json": '{"name":"widgets","dependencies":{"express":"^4.0.0"}}',
}

_CHANGE_REQUEST = {
    "id": "CR-1",
    "title": "Reject expired tokens on login",
    "kind": "bug",
    "description": "login() accepts an expired token; it must return 401 instead.",
    "acceptance_criteria": ["An expired token yields 401", "A valid token still succeeds"],
}


class _CloneExecutor(FakeExecutor):
    """A FakeExecutor that answers the git calls acquisition makes, and materialises the clone.

    ``git clone`` on a real executor populates the working tree; here it seeds ``files`` so the
    inventory that runs afterwards has something to survey — otherwise every test would be
    surveying an empty directory and the assertions would be vacuous.
    """

    def __init__(self, *, clone_ok: bool = True, default_branch: str = "main", **kw: Any) -> None:
        super().__init__(**kw)
        self._clone_ok = clone_ok
        self._default_branch = default_branch
        self.pushed: list[str] = []

    def run_command(self, cmd, cwd=".", timeout=None, env=None):  # type: ignore[override]
        self.commands.append(list(cmd))
        self.command_envs.append(env)
        joined = " ".join(cmd)
        if "clone" in joined:
            if not self._clone_ok:
                return RunResult(stdout="", stderr="fatal: repository not found", exit_code=128)
            self.files.update(_CLONED_FILES)
            return RunResult(stdout="", stderr="", exit_code=0)
        if "symbolic-ref" in joined:
            return RunResult(stdout=f"origin/{self._default_branch}\n", stderr="", exit_code=0)
        if "rev-parse" in joined:
            return RunResult(stdout=f"{_HEAD}\n", stderr="", exit_code=0)
        if "push" in joined:
            self.pushed.append(joined)
        return RunResult(stdout="", stderr="", exit_code=0)

    # Marks this as the local-disk executor: acquisition refuses to run on the sandbox executor,
    # which has no egress to github.com and so could neither clone the target nor push a result.
    def publish_feature(self, *a: Any, **kw: Any) -> RunResult:  # pragma: no cover - marker only
        return RunResult(stdout="", stderr="", exit_code=0)


def _state(**overrides: Any) -> dict:
    state = new_state(
        run_id="r1", attempt=0, project_id="p1",
        source_mode="brownfield", source_repo_url=_REPO_URL, change_request=dict(_CHANGE_REQUEST),
    )
    state.update(overrides)
    return state


@pytest.fixture
def executor():
    ex = _CloneExecutor()
    set_executor(ex)
    try:
        yield ex
    finally:
        set_executor(None)


# --- the lane itself ---------------------------------------------------------------------------


def test_route_entry_sends_brownfield_to_acquire_and_everything_else_to_scaffold() -> None:
    assert route_entry({"source_mode": "brownfield"}) == "acquire"
    assert route_entry({"source_mode": "greenfield"}) == "scaffold"
    # Absent / empty / a legacy checkpoint written before the field existed -> greenfield, so no
    # existing caller changes behaviour.
    assert route_entry({}) == "scaffold"
    assert route_entry({"source_mode": ""}) == "scaffold"
    assert route_entry(new_state(run_id="x", attempt=0)) == "scaffold"


def test_acquire_clones_surveys_and_records_the_baseline(executor: _CloneExecutor) -> None:
    out = acquire_repo_node(_state())

    assert out["workflow_status"] == "repo_acquired"
    assert out["base_sha"] == _HEAD
    assert out["base_branch"] == "main"
    assert out["branch"] == "sdlc/cr-r1"
    # repo_url is what every downstream node reads; source_repo_url keeps its own meaning.
    assert out["repo_url"] == _REPO_URL and out["source_repo_url"] == _REPO_URL

    inv = out["repo_inventory"]
    assert inv["source_files"] == ["src/api/routes.py", "src/auth/login.py"]
    assert inv["test_files"] == ["tests/test_login.py"]
    assert out["baseline_digests"]["src/auth/login.py"]        # a real digest was taken


def test_acquire_writes_nothing_to_the_repo(executor: _CloneExecutor) -> None:
    # THE regression test for this milestone. Every file that came out of the clone must be
    # byte-identical afterwards, and no boilerplate may have been rendered over it.
    acquire_repo_node(_state())

    for path, content in _CLONED_FILES.items():
        assert executor.files[path] == content, f"{path} was modified during acquisition"
    assert executor.writes == [], f"acquisition wrote files: {executor.writes}"


def test_acquire_never_creates_a_repo_or_pushes(executor: _CloneExecutor) -> None:
    # scaffold_node's publish_scaffold does `gh repo create` + `git push -u origin main`, which on a
    # clone fast-forwards the user's default branch. Acquisition must do neither.
    acquire_repo_node(_state())

    flattened = [" ".join(c) for c in executor.commands]
    assert not [c for c in flattened if "gh" in c and "repo create" in c]
    assert not [c for c in flattened if c.startswith("git push")]
    assert executor.pushed == []
    assert executor.commits == []


def test_working_branch_is_per_run_and_never_the_repos_own(executor: _CloneExecutor) -> None:
    # Never main/master (the default branch) and never `dev`, which on a real repo is usually a
    # SHARED integration branch — committing there would land on someone else's work, and finalize
    # would then open a PR diffing every unrelated commit already on it.
    out = acquire_repo_node(_state())
    assert out["branch"] not in {"main", "master", "dev", out["base_branch"]}
    assert out["branch"] == "sdlc/cr-r1"


def test_generated_code_stays_the_change_set_not_the_whole_repo(executor: _CloneExecutor) -> None:
    # documentation_node reads every generated_code entry into ONE prompt and packaging zips them,
    # so seeding a whole repo here would blow up both. The full listing lives on repo_inventory.
    out = acquire_repo_node(_state())
    assert out["generated_code"] == []
    assert len(out["repo_inventory"]["files"]) == len(_CLONED_FILES)


# --- preconditions: every one of these must fail BEFORE anything is cloned ---------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://github.com/acme/widgets",              # not https
        "https://gitlab.com/acme/widgets",             # not github
        "https://github.com/acme/widgets/tree/main",   # not a clone root
        "https://user:pw@github.com/acme/widgets",     # embedded credentials
        "file:///etc/passwd",
        "",
    ],
)
def test_disallowed_repo_urls_are_refused_without_cloning(
    executor: _CloneExecutor, bad_url: str
) -> None:
    # The host clone runs with the operator's own git/gh credentials. Until brownfield, nothing ever
    # handed it a caller-supplied URL — `git clone <anything>` with real credentials is exactly the
    # SSRF-shaped primitive the allowlist exists to stop.
    out = acquire_repo_node(_state(source_repo_url=bad_url))

    assert out["workflow_status"] == "needs_human_review"
    assert not [c for c in executor.commands if "clone" in " ".join(c)]
    assert "[acquire] FAILED" in out["generation_summary"]


def test_missing_change_request_is_refused(executor: _CloneExecutor) -> None:
    out = acquire_repo_node(_state(change_request={}))
    assert out["workflow_status"] == "needs_human_review"
    assert "nothing to implement" in out["generation_summary"]
    assert not [c for c in executor.commands if "clone" in " ".join(c)]


def test_sandbox_executor_is_refused_at_the_boundary() -> None:
    # MCPExecutor has no publish_feature and the exec-sandbox has no egress to github.com, so it
    # can neither clone the target nor push the result. Fail now, not 40 minutes in.
    plain = FakeExecutor()          # no publish_feature -> stands in for the sandbox executor
    set_executor(plain)
    try:
        out = acquire_repo_node(_state())
    finally:
        set_executor(None)

    assert out["workflow_status"] == "needs_human_review"
    assert "cannot reach GitHub" in out["generation_summary"]
    assert plain.commands == []


def test_clone_failure_is_reported_not_raised() -> None:
    ex = _CloneExecutor(clone_ok=False)
    set_executor(ex)
    try:
        out = acquire_repo_node(_state())
    finally:
        set_executor(None)

    assert out["workflow_status"] == "needs_human_review"
    assert "git clone failed" in out["generation_summary"]
    assert "repository not found" in out["generation_summary"]


def test_git_calls_are_non_interactive(executor: _CloneExecutor) -> None:
    # Without GIT_TERMINAL_PROMPT=0 a private or misspelled URL makes git BLOCK on a username
    # prompt against a dead stdin until the timeout — the run hangs instead of failing.
    acquire_repo_node(_state())

    clone_env = next(
        env for cmd, env in zip(executor.commands, executor.command_envs) if "clone" in " ".join(cmd)
    )
    assert clone_env["GIT_TERMINAL_PROMPT"] == "0"


def test_submodules_are_not_fetched(executor: _CloneExecutor) -> None:
    # A malicious .gitmodules would otherwise make the clone itself fetch arbitrary hosts.
    acquire_repo_node(_state())
    clone = next(c for c in executor.commands if "clone" in " ".join(c))
    assert "--no-recurse-submodules" in clone


def test_a_repo_with_no_readable_files_is_refused() -> None:
    class _EmptyClone(_CloneExecutor):
        def run_command(self, cmd, cwd=".", timeout=None, env=None):  # type: ignore[override]
            if "clone" in " ".join(cmd):
                self.commands.append(list(cmd))
                self.command_envs.append(env)
                return RunResult(stdout="", stderr="", exit_code=0)   # clones nothing
            return super().run_command(cmd, cwd, timeout, env)

    ex = _EmptyClone()
    set_executor(ex)
    try:
        out = acquire_repo_node(_state())
    finally:
        set_executor(None)

    assert out["workflow_status"] == "needs_human_review"
    assert "no readable files" in out["generation_summary"]


# --- the report -------------------------------------------------------------------------------


def test_change_report_records_the_request_and_what_was_found(executor: _CloneExecutor) -> None:
    out = acquire_repo_node(_state())

    report = out["change_report"]
    assert _CHANGE_REQUEST["title"] in report
    assert "An expired token yields 401" in report      # acceptance criteria survive
    assert _REPO_URL in report and _HEAD in report
    assert "sdlc/cr-r1" in report
    assert "Inventory" in report

    written = Path(out["change_report_path"])
    assert written.name == "change-report.md" and written.read_text(encoding="utf-8") == report


# --- through the COMPILED graph ----------------------------------------------------------------
# The node tests above call acquire_repo_node directly. This one proves the conditional entry edge
# is actually wired, i.e. that a brownfield run never reaches scaffold_node in the real graph.


_PLAN_JSON = (
    '{"work_items": [{"id": "auth-fix", "action": "modify",'
    ' "change_intent": "Reject an expired token in verify().",'
    ' "target_files": ["src/auth/login.py"],'
    ' "acceptance_criteria": ["An expired token is rejected"]}],'
    ' "notes": "Read login.py; the check belongs in verify()."}'
)
_EDITED = "def login():\n    return not expired()\n"


def _drive_lane(monkeypatch, ex: _CloneExecutor, *, edit_to: str | None = _EDITED, **state_kw):
    """Run the brownfield lane with a stub gateway that plans, then edits."""
    from app.graph.graph import workflow
    from app.services import llm_gateway as gw

    def complete_with_tools(prompt, *, system=None, tools=None, max_iters=6):
        by_name = {t.name: t for t in (tools or [])}
        if system and "Change Planner" in system:
            return _PLAN_JSON
        if edit_to is not None:                       # the Code Modifier's turn
            by_name["read_file"].handler(path="src/auth/login.py")
            by_name["write_file"].handler(path="src/auth/login.py", content=edit_to)
            return "Added the expiry check."
        return "I could not make this change safely."

    monkeypatch.setattr(gw.llm_gateway, "complete_with_tools", complete_with_tools)
    set_executor(ex)
    try:
        state = new_state(
            run_id=state_kw.pop("run_id", "lane1"), attempt=0, project_id="p1",
            source_mode="brownfield", source_repo_url=_REPO_URL,
            change_request=dict(_CHANGE_REQUEST), **state_kw,
        )
        config = {"configurable": {"thread_id": state["run_id"]}, "recursion_limit": 60}
        workflow.invoke(state, config)
        return workflow.get_state(config).values
    finally:
        set_executor(None)


def test_brownfield_run_through_the_graph_never_scaffolds(monkeypatch) -> None:
    """THE regression test for the whole design: a full brownfield run never touches scaffold_node.

    scaffold_node would overwrite .gitignore, README.md and package.json from DEFAULT_CAPABILITIES
    and then fast-forward them onto the user's default branch. Asserting the absence is the point,
    so this runs the ENTIRE lane (acquire -> plan -> edit -> gate -> commit) rather than stopping
    early — the further the run gets, the more chances there are to hit it.
    """
    ex = _CloneExecutor()
    final = _drive_lane(monkeypatch, ex, run_id="bf-graph-1")

    assert final["base_sha"] == _HEAD
    assert final["repo_inventory"]["source_files"]

    written = set(ex.writes)
    for boilerplate in ("p1/.gitignore", "p1/README.md", "p1/package.json", "p1/Dockerfile",
                        "p1/requirements.txt", "p1/docker-compose.yml"):
        assert boilerplate not in written, f"scaffold_node ran: it wrote {boilerplate}"
    assert ex.files["p1/.gitignore"] == _CLONED_FILES["p1/.gitignore"]
    assert ex.files["p1/README.md"] == _CLONED_FILES["p1/README.md"]
    assert ex.files["p1/package.json"] == _CLONED_FILES["p1/package.json"]
    assert not final.get("scaffold_files")

    flattened = [" ".join(c) for c in ex.commands]
    assert not [c for c in flattened if "repo create" in c]
    assert ex.pushed == []                      # push was not requested, so nothing left the box
    # Only the ONE planned file was written; everything else survived byte-identical.
    assert ex.writes == ["p1/src/auth/login.py"]


def test_greenfield_run_through_the_graph_is_unchanged() -> None:
    # The regression proof for the conditional entry: a run that sets no source_mode must still
    # scaffold exactly as before.
    from app.graph.graph import workflow

    ex = FakeExecutor()
    set_executor(ex)
    try:
        state = new_state(run_id="gf-graph-1", attempt=0, project_id="p2")
        config = {"configurable": {"thread_id": "gf-graph-1"}, "recursion_limit": 50}
        workflow.invoke(state, config)
        final = workflow.get_state(config).values
    finally:
        set_executor(None)

    assert final["workflow_status"] == "completed"
    assert final["scaffold_files"], "greenfield must still render boilerplate"
    assert any(p.endswith("README.md") for p in ex.writes)


def test_git_env_extends_the_real_environment_rather_than_replacing_it() -> None:
    """Regression: caught on the FIRST real run against GitHub, not by any unit test.

    ``subprocess(env=...)`` REPLACES the child environment, it does not extend it. Passing a bare
    dict of git switches stripped PATH/SystemRoot, so git could not resolve DNS at all and failed
    with "Could not resolve host: github.com" — indistinguishable from a network outage, on a
    machine whose network was fine.
    """
    import os

    from app.graph.nodes import _GIT_NONINTERACTIVE, _git_env

    env = _git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"                 # the switches are applied...
    for key, value in _GIT_NONINTERACTIVE.items():
        assert env[key] == value
    # ...on top of the ambient environment, not instead of it.
    assert set(os.environ).issubset(set(env)), "git would lose PATH/SystemRoot and fail to resolve DNS"


def test_every_git_call_in_acquisition_is_non_interactive_and_keeps_the_environment(
    executor: _CloneExecutor,
) -> None:
    import os

    acquire_repo_node(_state())

    git_envs = [
        env for cmd, env in zip(executor.commands, executor.command_envs)
        if cmd and cmd[0] == "git" and env is not None
    ]
    assert git_envs, "no git call passed an environment at all"
    for env in git_envs:
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "PATH" in env or "Path" in env, "a git call was given a stripped environment"
        assert set(os.environ).issubset(set(env))


# --- the full brownfield lane, end to end through the compiled graph ----------------------------
# acquire -> change_plan -> select -> code_modifier -> change_gate -> select -> change_commit
# -> finalize. Everything is faked (executor, LLM, GitHub) so this runs offline in seconds.


def test_the_whole_lane_edits_gates_and_commits(monkeypatch) -> None:
    ex = _CloneExecutor()
    final = _drive_lane(monkeypatch, ex, run_id="lane-happy")

    assert final["repo_inventory"]["source_files"]                 # acquired
    assert [i.id for i in final["work_items"]] == ["auth-fix"]     # planned
    assert ex.files["p1/src/auth/login.py"] == _EDITED             # edited
    assert final["gate_result"]["passed"] is True                  # gate proved it changed
    assert final["changed_files"] == ["p1/src/auth/login.py"]
    assert len(ex.commits) == 1                                    # committed
    # The shared tail (finalize -> package) runs for brownfield too, so the terminal status is the
    # same "completed" greenfield ends on; change_commit's own stamp is an intermediate marker.
    assert final["workflow_status"] == "completed"
    # Untargeted files are byte-identical — the point of the whole design.
    for path, content in _CLONED_FILES.items():
        if path != "p1/src/auth/login.py":
            assert ex.files[path] == content


def test_an_edit_that_changes_nothing_never_reaches_a_commit(monkeypatch) -> None:
    # THE silent-success failure: files_complete would pass here because the target exists.
    ex = _CloneExecutor()
    final = _drive_lane(monkeypatch, ex, edit_to=None, run_id="lane-noop")

    assert final["workflow_status"] == "needs_human_review"
    assert final.get("changed_files", []) == []
    assert ex.commits == []
    assert ex.files["p1/src/auth/login.py"] == _CLONED_FILES["p1/src/auth/login.py"]


def test_a_local_run_commits_but_never_pushes_or_opens_a_pr(monkeypatch) -> None:
    # Push is opt-in. Without it the diff is inspectable locally and nothing leaves the machine.
    ex = _CloneExecutor()
    final = _drive_lane(monkeypatch, ex, run_id="lane-local")

    assert len(ex.commits) == 1
    assert ex.pushed == []
    assert final["finalize_status"] == "skipped"
    assert not final.get("pr_url")
    assert "no remote branch" in final["generation_summary"]


def test_a_pushing_run_opens_a_DRAFT_pr_against_the_repos_own_default_branch(monkeypatch) -> None:
    from app.graph import nodes as nodes_module
    from app.integrations.github import FakeGitHubClient

    fake_github = FakeGitHubClient()
    monkeypatch.setattr(nodes_module, "get_github_client", lambda **_kw: fake_github)

    ex = _CloneExecutor(default_branch="master")
    final = _drive_lane(monkeypatch, ex, run_id="lane-push",
                        push_enabled=True, git_remote="acme/widgets")

    assert final["finalize_status"] == "pr_created"
    call = fake_github.calls[0]
    assert call["head"] == "sdlc/cr-lane-push"      # the run's own branch, never dev/master
    assert call["base"] == "master"                 # the repo's REAL default, not hardcoded main
    assert call["draft"] is True                    # a human must mark it ready; it cannot merge
    # The body must NOT be the security report: in brownfield that describes the USER's existing
    # code, and this PR may be public.
    assert "Reject expired tokens on login" in call["body"]
    assert "src/auth/login.py" in call["body"]
    assert "draft" in call["body"].lower()


# --- verification: does the change break what the repo could already do? -------------------------


def _drive_verified(monkeypatch, ex, *, baseline: str, after: str, **kw):
    """Drive the whole lane with the repo's own suite scripted: baseline -> after."""
    from app.graph import nodes as nodes_module
    from app.services.test_command import TestCommand, TestOutcome

    outcomes = iter([baseline, after])
    monkeypatch.setattr(nodes_module, "detect_test_command",
                        lambda *a, **k: TestCommand(argv=["x"], label="pytest", evidence="pyproject.toml"))
    monkeypatch.setattr(
        nodes_module, "run_tests",
        lambda *a, **k: TestOutcome(next(outcomes, after), "scripted", "pytest"),
    )
    return _drive_lane(monkeypatch, ex, **kw)


def test_a_change_that_breaks_the_suite_never_reaches_a_commit(monkeypatch) -> None:
    # THE point of verification. The suite passed before this change and fails after it, so the
    # change is ours and it is wrong — no commit, no branch pushed, no pull request.
    ex = _CloneExecutor()
    final = _drive_verified(monkeypatch, ex, baseline="passed", after="failed", run_id="v-regress")

    assert final["verify_verdict"] == "regressed"
    assert final["workflow_status"] == "needs_human_review"
    assert ex.commits == [] and ex.pushed == []
    assert "REGRESSION" in final["generation_summary"]
    # The edit itself still happened on the local branch, so a human can read the diff and judge.
    assert ex.files["p1/src/auth/login.py"] == _EDITED


def test_a_repo_whose_suite_was_already_red_is_still_changeable(monkeypatch) -> None:
    # Otherwise every repository arriving with a failing test would be permanently un-changeable.
    ex = _CloneExecutor()
    final = _drive_verified(monkeypatch, ex, baseline="failed", after="failed", run_id="v-pre")

    assert final["verify_verdict"] == "preexisting"
    assert len(ex.commits) == 1
    assert "not caused by this change" in final["generation_summary"].lower()


def test_a_verified_change_says_so(monkeypatch) -> None:
    ex = _CloneExecutor()
    final = _drive_verified(monkeypatch, ex, baseline="passed", after="passed", run_id="v-green")

    assert final["verify_verdict"] == "passed"
    assert len(ex.commits) == 1


def test_an_unrunnable_suite_commits_but_never_claims_verification(monkeypatch) -> None:
    # Nothing was proven either way. The change proceeds to a DRAFT PR, and the PR must say so.
    from app.graph.nodes import _verification_line

    ex = _CloneExecutor()
    final = _drive_verified(monkeypatch, ex, baseline="inconclusive", after="inconclusive",
                            run_id="v-unknown")

    assert final["verify_verdict"] == "unverified"
    assert len(ex.commits) == 1                       # not blocked...
    assert "NOT test-verified" in final["generation_summary"]   # ...but not claimed either
    assert "NOT test-verified" in _verification_line(final)
