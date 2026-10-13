"""``--only`` must select a slice that can boot, not just the matching handler/page.

Tree-structured packs name work items after directories (``backend-modules-auth``,
``frontend-pages``), so matching on the id alone selected nothing for ``--only login``; and even on
the older per-operation packs a match left out the entry point, config, db and models, so the
slice could be generated but never started.
"""

from __future__ import annotations

import pytest

from app.models import WorkItem

rf = pytest.importorskip("scripts.run_fixture")


def _item(item_id: str, endpoints: list[str] | None = None, screens: list[str] | None = None) -> WorkItem:
    return WorkItem(
        id=item_id, endpoints=endpoints or [], screens=screens or [],
        target_files=[f"{item_id}.txt"],
    )


PLAN = [
    _item("backend-root"),
    _item("backend-models"),
    _item("backend-modules-auth", endpoints=["POST /auth/login", "GET /auth/me"]),
    _item("backend-modules-vehicles", endpoints=["GET /vehicles"]),
    _item("backend-middleware"),
    _item("frontend-components-ui"),
    _item("frontend-pages", screens=["Login", "Vehicle List"]),
    _item("frontend-root"),
]


def _ids(items: list[WorkItem]) -> list[str]:
    return [w.id for w in items]


def test_matches_endpoints_and_screens_not_only_ids() -> None:
    selected, _ = rf._select_slice(PLAN, "login", exact=True)
    assert _ids(selected) == ["backend-modules-auth", "frontend-pages"]


def test_adds_same_side_infrastructure_in_plan_order() -> None:
    selected, infra = rf._select_slice(PLAN, "login")
    assert _ids(selected) == [
        "backend-root", "backend-models", "backend-modules-auth", "backend-middleware",
        "frontend-components-ui", "frontend-pages", "frontend-root",
    ]
    assert infra == ["backend-root", "backend-models", "backend-middleware",
                     "frontend-components-ui", "frontend-root"]
    # Another feature's handler is never pulled in: only items with no endpoints/screens are infra.
    assert "backend-modules-vehicles" not in _ids(selected)


def test_backend_only_match_does_not_pull_in_the_frontend() -> None:
    selected, infra = rf._select_slice(PLAN, "/vehicles")
    assert _ids(selected) == ["backend-root", "backend-models", "backend-modules-vehicles", "backend-middleware"]
    assert all(i.startswith("backend") for i in infra)


def test_no_match_returns_nothing() -> None:
    assert rf._select_slice(PLAN, "does-not-exist") == ([], [])


def test_exact_keeps_the_old_behaviour() -> None:
    selected, infra = rf._select_slice(PLAN, "backend-modules-auth", exact=True)
    assert _ids(selected) == ["backend-modules-auth"]
    assert infra == []
