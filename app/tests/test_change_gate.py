"""Proving a change actually happened — and that ONLY the change happened.

``files_complete``, the only gate the pipeline had, asks whether every target path exists. In
brownfield every target already exists, so it passes whether or not the editing agent wrote
anything: a run could report success with a zero-line diff. These tests pin the two questions that
replace it, both answered from content digests taken before any edit.

The second question is the one with teeth. Whole-file overwrites make collateral damage easy — the
model reads three files to understand one, and rewrites all three — and a reviewer skimming a large
diff will not reliably catch it. Nothing else in the pipeline would.

Pure executor reads, no LLM, no git.
"""

from __future__ import annotations

from app.integrations.executor import FakeExecutor
from app.services.change_gate import (
    classify_regressions,
    current_digests,
    digest_of,
    evaluate,
)

_BEFORE = {
    "src/auth/token.py": "def verify(t):\n    return True\n",
    "src/auth/login.py": "from .token import verify\n",
    "README.md": "# app\n",
}


def _executor(**overrides: str) -> FakeExecutor:
    files = {f"p1/{k}": v for k, v in {**_BEFORE, **overrides}.items()}
    return FakeExecutor(files=files)


def _baseline() -> dict[str, str]:
    return {rel: digest_of(content) for rel, content in _BEFORE.items()}


def _evaluate(executor, *, action="modify", targets=("src/auth/token.py",), claimed=None):
    return evaluate(
        executor, "p1",
        action=action,
        target_files=list(targets),
        baseline_digests=_baseline(),
        claimed_paths=set(claimed if claimed is not None else targets),
    )


# --- did the target change? ---------------------------------------------------------------------


def test_an_untouched_modify_target_fails_the_gate() -> None:
    # THE reason this gate exists. files_complete passes here — the file is right there — so
    # without this check a run reports success having changed nothing at all.
    result = _evaluate(_executor())

    assert result["passed"] is False
    failed = [c for c in result["checks"] if not c["passed"]]
    assert [c["name"] for c in failed] == ["files_changed"]
    assert "no change detected in: src/auth/token.py" in failed[0]["stderr"]


def test_a_real_edit_passes() -> None:
    result = _evaluate(_executor(**{"src/auth/token.py": "def verify(t):\n    return check(t)\n"}))
    assert result["passed"] is True
    assert all(c["passed"] for c in result["checks"])


def test_a_modify_target_that_vanished_fails() -> None:
    ex = _executor()
    del ex.files["p1/src/auth/token.py"]
    result = _evaluate(ex)
    assert result["passed"] is False
    assert any("not written" in c["stderr"] for c in result["checks"] if not c["passed"])


def test_a_create_target_must_now_exist() -> None:
    ex = _executor()
    missing = _evaluate(ex, action="create", targets=("src/auth/new.py",))
    assert missing["passed"] is False
    assert any("not written" in c["stderr"] for c in missing["checks"] if not c["passed"])

    ex.files["p1/src/auth/new.py"] = "x = 1\n"
    created = _evaluate(ex, action="create", targets=("src/auth/new.py",))
    assert created["passed"] is True


def test_the_failure_text_tells_the_agent_what_to_do() -> None:
    # It is fed back verbatim to the modifier on a retry, so it has to be actionable.
    failed = [c for c in _evaluate(_executor())["checks"] if not c["passed"]][0]
    assert "Re-read them" in failed["stderr"] or "apply the change" in failed["stderr"]


# --- did anything ELSE change? ------------------------------------------------------------------


def test_a_file_no_work_item_claimed_fails_the_gate() -> None:
    # Collateral damage: the model rewrote a file it was only supposed to read. Whole-file
    # overwrites make this easy, and nothing else in the pipeline would catch it.
    ex = _executor(**{
        "src/auth/token.py": "def verify(t):\n    return check(t)\n",     # the intended edit
        "README.md": "# app\n\nRewritten for no reason.\n",               # the collateral one
    })
    result = _evaluate(ex)

    assert result["passed"] is False
    collateral = [c for c in result["checks"] if c["name"] == "no_collateral_changes"]
    assert collateral[0]["passed"] is False
    assert "README.md" in collateral[0]["stderr"]


def test_a_file_another_work_item_claimed_is_not_collateral_damage() -> None:
    # A two-item plan edits two files. Item A's gate must not fail because item B did its job.
    ex = _executor(**{
        "src/auth/token.py": "def verify(t):\n    return check(t)\n",
        "src/auth/login.py": "from .token import verify\n# item B's edit\n",
    })
    result = _evaluate(ex, claimed=("src/auth/token.py", "src/auth/login.py"))
    assert result["passed"] is True


def test_collateral_paths_are_listed_and_overflow_is_reported() -> None:
    extra = {f"noise/f{i}.py": "x\n" for i in range(14)}
    baseline = {**_baseline(), **{k: digest_of(v) for k, v in extra.items()}}
    files = {f"p1/{k}": v for k, v in {**_BEFORE, **extra}.items()}
    files["p1/src/auth/token.py"] = "changed\n"
    for i in range(14):
        files[f"p1/noise/f{i}.py"] = "tampered\n"

    result = evaluate(
        FakeExecutor(files=files), "p1", action="modify",
        target_files=["src/auth/token.py"], baseline_digests=baseline,
        claimed_paths={"src/auth/token.py"},
    )
    stderr = [c["stderr"] for c in result["checks"] if c["name"] == "no_collateral_changes"][0]
    assert "+4 more" in stderr           # 14 collateral files, first 10 named


def test_gate_result_has_the_same_shape_as_the_greenfield_gate() -> None:
    # So repair accounting, routing and escalation work over it unchanged.
    result = _evaluate(_executor())
    assert set(result) == {"passed", "checks"}
    for check in result["checks"]:
        assert set(check) == {"name", "passed", "stderr", "stdout", "exit_code", "scope"}


def test_digests_are_content_addressed() -> None:
    ex = _executor()
    first = current_digests(ex, "p1", ["src/auth/token.py"])
    ex.files["p1/src/auth/token.py"] = "different\n"
    assert current_digests(ex, "p1", ["src/auth/token.py"]) != first
    assert current_digests(ex, "p1", ["gone.py"]) == {"gone.py": ""}


# --- regression classification -------------------------------------------------------------------


def test_only_a_check_that_used_to_pass_counts_as_a_regression() -> None:
    # A brownfield repo is not obliged to be green when we find it. Without this split, a repo with
    # already-failing tests burns the whole debug budget on code the run never touched.
    got = classify_regressions(
        baseline={"compile": True, "build": True, "test": False},
        current={"compile": True, "build": False, "test": False},
    )
    assert got["regressed"] == ["build"]         # passed before, fails now -> ours
    assert got["preexisting"] == ["test"]        # was already failing -> not ours
    assert got["fixed"] == []


def test_a_check_fixed_by_the_change_is_reported_as_fixed() -> None:
    got = classify_regressions(baseline={"test": False}, current={"test": True})
    assert got["fixed"] == ["test"] and got["regressed"] == []


def test_a_check_with_no_baseline_is_unknown_not_assumed_green() -> None:
    # Claiming "no regression" about a check that never ran before would be an unearned assurance.
    got = classify_regressions(baseline={}, current={"lint": False})
    assert got["unknown"] == ["lint"] and got["regressed"] == []


# --- the verify node: what the tests did and did not prove ---------------------------------------
# The change gate proves the right FILES changed. This proves the change did not BREAK what the
# repository could already do — and, just as importantly, is honest when it cannot prove anything.


def _verify(monkeypatch, *, baseline: str, after: str):
    """Drive change_verify_node with a scripted baseline and post-change outcome."""
    from app.graph import nodes as nodes_module
    from app.integrations.executor import set_executor
    from app.services.test_command import TestCommand, TestOutcome

    monkeypatch.setattr(nodes_module, "detect_test_command",
                        lambda *a, **kw: TestCommand(argv=["x"], label="pytest", evidence="e"))
    monkeypatch.setattr(nodes_module, "run_tests",
                        lambda *a, **kw: TestOutcome(after, f"{after} summary", "pytest"))

    ex = FakeExecutor()
    set_executor(ex)
    try:
        return nodes_module.change_verify_node({
            "project_id": "p1", "run_id": "r1", "generation_summary": "",
            "baseline_test": {"status": baseline, "summary": f"{baseline} before"},
            "test_command": "pytest (pyproject.toml)",
            "repo_inventory": {"files": ["a.py"]},
        })
    finally:
        set_executor(None)


def test_a_suite_that_passed_before_and_fails_now_is_a_regression(monkeypatch) -> None:
    out = _verify(monkeypatch, baseline="passed", after="failed")
    assert out["verify_verdict"] == "regressed"
    assert "REGRESSION" in out["generation_summary"]


def test_a_suite_that_was_already_failing_is_not_blamed_on_the_change(monkeypatch) -> None:
    # Without this, every repo arriving with a red suite would be un-changeable.
    out = _verify(monkeypatch, baseline="failed", after="failed")
    assert out["verify_verdict"] == "preexisting"
    assert "not caused by this change" in out["generation_summary"].lower()


def test_a_change_that_fixes_a_failing_suite_is_reported_as_fixed(monkeypatch) -> None:
    out = _verify(monkeypatch, baseline="failed", after="passed")
    assert out["verify_verdict"] == "fixed"


def test_a_passing_suite_is_a_pass(monkeypatch) -> None:
    out = _verify(monkeypatch, baseline="passed", after="passed")
    assert out["verify_verdict"] == "passed"


def test_a_suite_that_cannot_run_now_is_unverified_not_a_regression(monkeypatch) -> None:
    # A missing dev dependency must not read as "your change broke the tests".
    out = _verify(monkeypatch, baseline="passed", after="inconclusive")
    assert out["verify_verdict"] == "unverified"
    assert "NOT" in out["generation_summary"]


def test_no_conclusive_baseline_means_nothing_can_be_proven(monkeypatch) -> None:
    out = _verify(monkeypatch, baseline="inconclusive", after="passed")
    assert out["verify_verdict"] == "unverified"
    assert "NOT test-verified" in out["generation_summary"]


def test_only_a_regression_blocks_the_commit() -> None:
    from app.graph.router import route_after_change_verify

    assert route_after_change_verify({"verify_verdict": "regressed"}) == "escalate"
    for ok in ("passed", "fixed", "preexisting", "unverified"):
        assert route_after_change_verify({"verify_verdict": ok}) == "change_commit"
    assert route_after_change_verify({}) == "change_commit"


def test_an_unverified_run_says_so_loudly_in_the_pr_body() -> None:
    # The most damaging thing the report could do is imply verification that never happened.
    from app.graph.nodes import _verification_line

    unverified = _verification_line({"verify_verdict": "unverified",
                                     "verify_test": {"summary": "pytest collected no tests"},
                                     "test_command": "pytest"})
    assert "NOT test-verified" in unverified
    assert "run the tests yourself" in unverified

    passed = _verification_line({"verify_verdict": "passed",
                                 "verify_test": {"summary": "69 passed"},
                                 "test_command": "pytest"})
    assert "tests pass" in passed and "NOT" not in passed
