"""Acceptance for the plan builder + manifest gate, wired to the REAL design-pack fixtures.

Pure disk/CSV/JSON work — no model, no sandbox (runs under `pytest -m "not integration"`).
"""

import re
from pathlib import Path

from app.models import WorkItem
from app.services.manifest_gate import check_manifest
from app.services.plan_builder import build_plan


def _requirement_ids(pack_dir: Path) -> set[str]:
    text = (pack_dir / "extracted-requirements.md").read_text(encoding="utf-8")
    return set(re.findall(r"\b(?:REQ|NFR|BR)-\d{3}\b", text))


# --- manifest gate ---------------------------------------------------------

def test_manifest_complete_pack_ok(dummy_pack_complete: Path) -> None:
    assert check_manifest(dummy_pack_complete) == {"ok": True, "missing": []}


def test_manifest_missing_pack_reports_D1_D2(dummy_pack_missing: Path) -> None:
    result = check_manifest(dummy_pack_missing)
    assert result["ok"] is False
    assert result["missing"] == ["D1", "D2"]  # schema.sql + openapi.yaml


def test_manifest_checks_disk_not_the_manifest_claim(dummy_pack_missing: Path) -> None:
    # A decoy manifest asserts 20/20 present; the gate must ignore the claim and check disk.
    assert (dummy_pack_missing / "index.false-claim.md").exists()
    assert "20/20 present" in (dummy_pack_missing / "index.false-claim.md").read_text(encoding="utf-8")
    assert check_manifest(dummy_pack_missing)["missing"] == ["D1", "D2"]


# --- plan builder ----------------------------------------------------------

def test_plan_items_validate_as_workitems(dummy_pack_complete: Path) -> None:
    plan = build_plan(dummy_pack_complete)
    assert plan and all(isinstance(item, WorkItem) for item in plan)


def test_plan_contains_both_login_items(dummy_pack_complete: Path) -> None:
    plan = build_plan(dummy_pack_complete)

    backend = next(item for item in plan if item.id == "backend-loginUser")
    assert "POST /auth/login" in backend.endpoints
    assert "REQ-002" in backend.requirement_ids

    frontend = next(item for item in plan if item.id == "frontend-login")
    assert "Login" in frontend.screens
    assert "REQ-002" in frontend.requirement_ids


def test_plan_has_no_orphan_requirement_ids(dummy_pack_complete: Path) -> None:
    valid = _requirement_ids(dummy_pack_complete)
    for item in build_plan(dummy_pack_complete):
        for req_id in item.requirement_ids:
            assert req_id in valid, f"{item.id} cites unknown requirement {req_id}"


# --- conftest fixtures instantiate against the real pack -------------------

def test_dummy_plan_fixture_loads(dummy_plan: list[WorkItem]) -> None:
    assert any(item.id == "frontend-login" for item in dummy_plan)
    assert any(item.id == "backend-loginUser" for item in dummy_plan)


def test_design_package_fixture_parses_pack(design_package: dict) -> None:
    assert "schema.sql" in design_package                      # text artifact
    assert isinstance(design_package["validation-rules.json"], dict)  # parsed JSON artifact


def test_fake_gateway_fixture_instantiates(fake_gateway) -> None:
    assert hasattr(fake_gateway, "complete")
    assert hasattr(fake_gateway, "complete_with_tools")
