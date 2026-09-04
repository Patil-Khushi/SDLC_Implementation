"""Surveying an EXISTING codebase — brownfield's answer to parsing a design pack.

Deterministic and executor-only: no LLM, no sandbox, no network. A seeded ``FakeExecutor`` IS a
repository here, which is the whole point of putting ``list_files`` on the interface — before that,
enumerating a tree meant an ad-hoc ``git ls-files`` the fake could not answer.

The posture under test is "degrade to empty, never guess": an unreadable tree yields an empty
inventory (a legible stop) rather than a partial one that would send the change planner after files
that do not exist.
"""

from __future__ import annotations

from app.integrations.executor import FakeExecutor, RunResult
from app.services.repo_inventory import (
    RepoInventory,
    detect_default_branch,
    digests,
    inventory,
)

_REPO = {
    "p/src/auth/login.py": "def login():\n    return 1\n",
    "p/src/auth/token.py": "TOKEN = 'x'\n",
    "p/src/api/routes.js": "module.exports = {}\n",
    "p/tests/test_login.py": "def test_login(): pass\n",
    "p/src/auth/login.test.js": "it('works', () => {})\n",
    "p/package.json": "{}",
    "p/requirements.txt": "fastapi\n",
    "p/README.md": "# hi\n",
    "p/migrations/001_init.py": "-- generated\n",
}


def test_inventory_separates_source_from_tests_and_config() -> None:
    inv = inventory(FakeExecutor(files=dict(_REPO)), "p")

    assert inv.source_files == ["src/api/routes.js", "src/auth/login.py", "src/auth/token.py"]
    assert inv.test_files == ["src/auth/login.test.js", "tests/test_login.py"]
    # README/package.json/requirements.txt are inventoried but are not change targets; a generated
    # migrations/ tree is neither.
    assert "README.md" in inv.files and "README.md" not in inv.source_files
    assert "migrations/001_init.py" not in inv.source_files


def test_inventory_groups_source_into_modules() -> None:
    inv = inventory(FakeExecutor(files=dict(_REPO)), "p")
    assert inv.by_dir == {
        "src/api": ["src/api/routes.js"],
        "src/auth": ["src/auth/login.py", "src/auth/token.py"],
    }


def test_inventory_reports_language_mix_and_package_managers() -> None:
    inv = inventory(FakeExecutor(files=dict(_REPO)), "p")
    assert inv.languages == {".py": 2, ".js": 1}
    assert inv.primary_language == ".py"
    assert inv.manifests == {"package.json": "npm", "requirements.txt": "pip"}


def test_empty_inventory_for_a_tree_that_is_not_there() -> None:
    # "Nothing recognizable here" must be a legible stop, not a crash and not a partial guess.
    inv = inventory(FakeExecutor(files=dict(_REPO)), "no-such-project")
    assert inv.is_empty and inv.source_files == [] and inv.primary_language == ""


def test_inventory_survives_an_executor_that_raises() -> None:
    class _Exploding(FakeExecutor):
        def list_files(self, project_dir, prefix=""):  # type: ignore[override]
            raise RuntimeError("boom")

    assert inventory(_Exploding(), "p").is_empty


def test_as_dict_is_plain_json_shaped_data() -> None:
    # It goes onto WorkflowState, and the checkpointer only allow-lists WorkItem for msgpack —
    # anything else must already be primitives or it deserialises wrong after a round-trip.
    import json

    payload = inventory(FakeExecutor(files=dict(_REPO)), "p").as_dict()
    assert json.loads(json.dumps(payload))["primary_language"] == ".py"


def test_render_reports_module_overflow_instead_of_truncating_silently() -> None:
    files = {f"p/src/m{i:03d}/a.py": "x" for i in range(70)}
    rendered = inventory(FakeExecutor(files=files), "p").render(max_paths=10)
    assert "+60 more modules" in rendered          # the count is stated, not quietly dropped


def test_render_handles_an_empty_repo() -> None:
    assert RepoInventory().render() == "_No files found._"


# --- digests: the baseline that makes "did anything change?" answerable ------------------------


def test_digests_are_content_addressed_and_stable() -> None:
    ex = FakeExecutor(files={"p/a.py": "one", "p/b.py": "one"})
    got = digests(ex, "p", ["a.py", "b.py"])
    assert got["a.py"] == got["b.py"]                 # same content -> same digest
    assert len(got["a.py"]) == 64

    ex.files["p/a.py"] = "two"
    assert digests(ex, "p", ["a.py"])["a.py"] != got["a.py"]


def test_digests_keep_unreadable_paths_as_empty_rather_than_dropping_them() -> None:
    # Absent and unreadable are both "no baseline"; keeping the key means a later comparison still
    # sees the path, instead of it silently vanishing from the change set.
    got = digests(FakeExecutor(files={"p/a.py": "x"}), "p", ["a.py", "gone.py"])
    assert got["gone.py"] == "" and got["a.py"] != ""


# --- default branch: what finalize opens its PR against ---------------------------------------


def _branch_executor(responses: dict[str, RunResult]) -> FakeExecutor:
    def script(cmd: list[str]) -> RunResult | None:
        for key, res in responses.items():
            if key in " ".join(cmd):
                return res
        return RunResult(stdout="", stderr="", exit_code=1)

    return FakeExecutor(command_results=script)


def test_default_branch_read_from_origin_head() -> None:
    ex = _branch_executor({"symbolic-ref": RunResult(stdout="origin/master\n", stderr="", exit_code=0)})
    assert detect_default_branch(ex, "p") == "master"


def test_default_branch_falls_back_to_remote_show() -> None:
    ex = _branch_executor({
        "remote show": RunResult(stdout="  HEAD branch: develop\n", stderr="", exit_code=0),
    })
    assert detect_default_branch(ex, "p") == "develop"


def test_default_branch_falls_back_to_main_when_nothing_answers() -> None:
    # Hardcoding "main" is only right for a repo this service scaffolded — but it is still the
    # right LAST resort, since a PR needs some base.
    assert detect_default_branch(_branch_executor({}), "p") == "main"
