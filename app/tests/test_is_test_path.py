"""``plan_builder._is_test_path`` — the shared test-file detector used by plan_builder's own leaf
exclusion (``_source_leaves``, ``_is_api_service``) AND, since a recent fix, by the Debugging
agent's report labelling (``app/agents/debugging.py``).

Regression coverage for a review-flagged gap: the suffix check
(``base.endswith((".test", "_test"))``) ran against the FULL basename, extension included, so it
only ever matched an extensionless ``foo_test`` — never the actual ``*_test.py`` convention
(``login_test.py``) it was meant to catch. The prefix form (``test_*.py``) was already correct.
"""

from __future__ import annotations

from app.services.plan_builder import _is_test_path


def test_python_prefix_convention() -> None:
    assert _is_test_path("app/api/test_login.py") is True
    assert _is_test_path("test_login.py") is True


def test_python_suffix_convention_previously_undetected() -> None:
    # The actual bug: this basename ends in ".py", not "_test", so the old endswith((".test",
    # "_test")) check against the full basename never matched it.
    assert _is_test_path("app/api/login_test.py") is True
    assert _is_test_path("login_test.py") is True


def test_js_colocated_conventions() -> None:
    assert _is_test_path("src/components/Button.test.jsx") is True
    assert _is_test_path("src/components/Button.spec.ts") is True


def test_tests_directory_segment() -> None:
    assert _is_test_path("app/tests/test_helpers.py") is True
    assert _is_test_path("src/__tests__/Button.js") is True


def test_ordinary_source_is_not_a_test_path() -> None:
    for path in (
        "app/api/login.py",
        "src/components/Button.jsx",
        "backend/src/routes/orders.controller.js",
        "src/utils/testable.js",       # "test" as a substring only, not the convention
        "src/contest_entries.py",      # "test" embedded mid-word must not false-positive
    ):
        assert _is_test_path(path) is False, f"{path} should not be a test path"


def test_extensionless_form_still_matches_for_backward_compatibility() -> None:
    # The pre-fix behaviour this suffix form originally targeted — still covered, just no longer
    # the ONLY form that works.
    assert _is_test_path("scripts/smoke_test") is True
