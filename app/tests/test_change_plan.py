"""Validating a proposed change plan before anything acts on it.

The planner is an LLM choosing files in somebody else's repository, and everything downstream
trusts its answer, so the plan is treated as a proposal and checked here first. The failure mode
these tests exist for is quiet: a plan naming plausible files the planner never opened looks
exactly like a good one.

Pure functions over a file list — no LLM, no executor, no network.
"""

from __future__ import annotations

import pytest

from app.models import WorkItem
from app.services.change_plan import (
    MAX_FILES_PER_CHANGE,
    MAX_ITEMS_PER_CHANGE,
    render_plan,
    validate_plan,
)

_REPO = {
    "src/auth/login.py",
    "src/auth/token.py",
    "src/api/routes.py",
    "tests/test_login.py",
    "package.json",
    ".gitignore",
    ".github/workflows/ci.yml",
    "assets/logo.png",
}


def _item(**kw) -> WorkItem:
    base = {
        "id": "auth-expiry",
        "action": "modify",
        "change_intent": "reject expired tokens",
        "target_files": ["src/auth/token.py"],
    }
    base.update(kw)
    return WorkItem(**base)


def test_a_well_formed_plan_passes() -> None:
    items, errors = validate_plan([_item()], _REPO)
    assert errors == []
    assert items[0].id == "auth-expiry"


def test_modify_on_a_file_that_is_not_in_the_repo_is_rejected() -> None:
    # The planner hallucinated a path. Left alone, the editing agent would CREATE it — quietly
    # adding a stray file instead of making the requested change.
    _, errors = validate_plan([_item(target_files=["src/auth/nope.py"])], _REPO)
    assert any("not in the repository" in e for e in errors)


def test_create_on_a_file_that_already_exists_is_rejected() -> None:
    # The reverse mistake: a whole-file overwrite of code nobody asked to replace.
    _, errors = validate_plan([_item(action="create", target_files=["src/auth/login.py"])], _REPO)
    assert any("already exists" in e for e in errors)


def test_delete_is_refused_in_v1() -> None:
    _, errors = validate_plan([_item(action="delete")], _REPO)
    assert any("'delete' is not supported" in e for e in errors)


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "/etc/passwd",
        "C:/Windows/system32/x.py",
        "src/../../escape.py",
    ],
)
def test_paths_outside_the_project_are_rejected(path: str) -> None:
    _, errors = validate_plan([_item(target_files=[path])], _REPO)
    assert any("outside the project" in e for e in errors), f"{path} was allowed"


@pytest.mark.parametrize(
    "path,fragment",
    [
        (".github/workflows/ci.yml", "CI workflow"),
        (".gitignore", "ignore rules"),
        (".env", "environment"),
        ("config/secrets/keys.py", "secrets directory"),
        ("certs/server.pem", "private key"),
        ("infra/main.tf", "infrastructure"),
        ("package-lock.json", "lockfile"),
        ("Dockerfile", "container"),
        ("assets/logo.png", "binary"),
    ],
)
def test_forbidden_targets_are_refused(path: str, fragment: str) -> None:
    # Two of these are load-bearing rather than tidy: a merged .github/workflows edit can read repo
    # secrets, and replacing .gitignore un-ignores whatever it protected.
    _, errors = validate_plan([_item(target_files=[path])], _REPO | {path})
    assert any(fragment in e for e in errors), f"{path} was allowed: {errors}"


def test_two_items_may_not_claim_the_same_file() -> None:
    # The second edit would overwrite the first, and the per-item gate could not attribute either.
    items = [_item(id="a"), _item(id="b")]
    _, errors = validate_plan(items, _REPO)
    assert any("claimed by both" in e for e in errors)


def test_duplicate_item_ids_are_rejected() -> None:
    items = [_item(id="same", target_files=["src/auth/token.py"]),
             _item(id="same", target_files=["src/auth/login.py"])]
    _, errors = validate_plan(items, _REPO)
    assert any("duplicate work-item id" in e for e in errors)


def test_an_item_with_no_targets_is_rejected() -> None:
    _, errors = validate_plan([_item(target_files=[])], _REPO)
    assert any("changes nothing" in e for e in errors)


def test_an_item_with_no_intent_is_rejected() -> None:
    # Without it the editing agent has a file list and no instruction.
    _, errors = validate_plan([_item(change_intent="   ")], _REPO)
    assert any("no change_intent" in e for e in errors)


def test_an_empty_plan_is_an_error() -> None:
    _, errors = validate_plan([], _REPO)
    assert errors == ["the planner produced no work items"]


def test_too_many_files_is_refused() -> None:
    # v1 is scoped to localized changes; beyond this the service cannot verify what it changed.
    repo = {f"src/m{i}.py" for i in range(MAX_FILES_PER_CHANGE + 2)} | _REPO
    item = _item(target_files=[f"src/m{i}.py" for i in range(MAX_FILES_PER_CHANGE + 1)])
    _, errors = validate_plan([item], repo)
    assert any("more than the" in e and "files" in e for e in errors)


def test_too_many_items_is_refused() -> None:
    repo = {f"src/m{i}.py" for i in range(MAX_ITEMS_PER_CHANGE + 2)} | _REPO
    items = [_item(id=f"i{i}", target_files=[f"src/m{i}.py"])
             for i in range(MAX_ITEMS_PER_CHANGE + 1)]
    _, errors = validate_plan(items, repo)
    assert any("work items" in e for e in errors)


def test_every_problem_is_reported_not_just_the_first() -> None:
    # A human fixing a rejected plan should see the whole picture, not one issue per run.
    items = [_item(id="a", action="modify", target_files=["src/nope.py"]),
             _item(id="a", action="create", target_files=["src/auth/login.py"])]
    _, errors = validate_plan(items, _REPO)
    assert len(errors) >= 3          # duplicate id + missing target + create-over-existing


def test_windows_separators_are_normalized() -> None:
    _, errors = validate_plan([_item(target_files=["src\\auth\\token.py"])], _REPO)
    assert errors == []


# --- report rendering ---------------------------------------------------------------------------


def test_render_shows_the_plan_and_its_blast_radius() -> None:
    impacts = {"src/auth/token.py": ["src/auth/login.py", "src/api/routes.py"]}
    out = render_plan([_item()], [], impacts)
    assert "auth-expiry" in out and "reject expired tokens" in out
    assert "`src/auth/login.py`" in out and "`src/api/routes.py`" in out


def test_render_states_rejection_reasons() -> None:
    out = render_plan([_item()], ["something was wrong"], {})
    assert "Plan REJECTED" in out and "something was wrong" in out


def test_render_reports_impact_overflow_rather_than_truncating_silently() -> None:
    impacts = {"src/auth/token.py": [f"src/dep{i}.py" for i in range(9)]}
    out = render_plan([_item()], [], impacts)
    assert "+4 more" in out


# --- path normalization ------------------------------------------------------------------------
# Found by adversarial review, and independently by two reviewers: every check below validate_plan's
# escape test used to run on the RAW path while _escapes_project normalized only internally. Since
# the forbidden patterns are anchored (^\.git, ^\.github/workflows/) and the existence test is a set
# membership, any prefix that collapses away defeated BOTH at once.


@pytest.mark.parametrize(
    "path",
    [
        "src/../.github/workflows/ci.yml",
        "./.github/workflows/evil.yml",
        ".//.github/workflows/evil.yml",
        "src/../.git/hooks/pre-commit",
        "./.git/config",
    ],
)
def test_dot_segments_cannot_launder_a_forbidden_path(path: str) -> None:
    _, errors = validate_plan([_item(action="create", target_files=[path])], _REPO)
    assert errors, f"{path} was ACCEPTED - the forbidden-path guard was bypassed"
    assert "not editable" in errors[0]


@pytest.mark.parametrize(
    "path",
    ["./src/auth/login.py", "src/auth/../auth/login.py", "src//auth//login.py"],
)
def test_dot_segments_cannot_hide_that_a_create_target_already_exists(path: str) -> None:
    # Otherwise 'create' silently becomes a whole-file overwrite of code nobody asked to replace.
    _, errors = validate_plan([_item(action="create", target_files=[path])], _REPO)
    assert any("already exists" in e for e in errors), f"{path} was ACCEPTED"


def test_two_spellings_of_one_file_still_collide() -> None:
    items = [_item(id="a", target_files=["src/auth/login.py"]),
             _item(id="b", target_files=["./src/auth/login.py"])]
    _, errors = validate_plan(items, _REPO)
    assert any("claimed by both" in e for e in errors)


def test_a_legitimate_dot_prefixed_modify_is_not_falsely_rejected() -> None:
    # The same normalization must not break the honest case: ./x and x are the same file.
    _, errors = validate_plan([_item(action="modify", target_files=["./src/auth/login.py"])], _REPO)
    assert errors == []


def test_the_reported_path_is_the_canonical_one() -> None:
    # A human reading the rejection needs the path that would actually have been written.
    _, errors = validate_plan(
        [_item(action="create", target_files=["src/../.github/workflows/x.yml"])], _REPO
    )
    assert "'.github/workflows/x.yml'" in errors[0]


def test_render_distinguishes_not_analysed_from_no_dependents() -> None:
    # "-" is an affirmative "nothing imports this". A target the graph never covered must not make
    # that claim — the blast radius is the whole point of the report.
    analysed = render_plan([_item()], [], {"src/auth/token.py": []})
    assert "| - |" in analysed

    unanalysed = render_plan([_item()], [], {})
    assert "not analysed" in unanalysed


def test_render_looks_up_impacts_under_the_canonical_path() -> None:
    # The producer keys on the normalized form; a raw-vs-normalized mismatch here would silently
    # render "-" for a file whose dependents were computed perfectly well.
    out = render_plan([_item(target_files=["./src/auth/token.py"])], [],
                      {"src/auth/token.py": ["src/auth/login.py"]})
    assert "`src/auth/login.py`" in out
