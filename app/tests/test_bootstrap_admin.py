"""A login with no way to create the first user: the pipeline adds a seed command, declares its
variables, and the README says how to use it — all from ONE answer (``bootstrap_plan``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.boilerplate import render_scaffold
from app.services.pack_index import load_pack
from app.services.pack_preflight import bootstrap_plan
from app.services.plan_builder import build_plan

_REPO = Path(__file__).resolve().parents[2]
FLEET, ECOM, AUTH = (_REPO / "uploads" / "v1", _REPO / "fixtures" / "resources", _REPO / "uploads" / "authentication")

SQL = "CREATE TABLE users (id uuid PRIMARY KEY, email text, password_hash text, role text,\n  CHECK (role IN ('owner', 'clerk')));"
SPEC = {"openapi": "3.0.3", "paths": {"/auth/login": {"post": {"operationId": "login", "responses": {"200": {"description": "ok"}}}},
                                       "/items": {"get": {"operationId": "listItems", "responses": {"200": {"description": "ok"}}}}}}
TREE = {"tree": {"backend/": {"src/": {"main.py": "FastAPI app factory", "__init__.py": "pkg"}}}}


def test_a_login_without_user_creation_gets_a_seed_plan_with_the_canonical_admin_role():
    plan = bootstrap_plan({"db_schema.sql": SQL, "openapi.json": SPEC, "backend-structure.json": TREE})
    assert plan["user_table"] == "users" and plan["role"] == "owner"       # no "admin" value: the first stored one
    assert plan["path"] == "backend/src/seed.py" and plan["command"] == "python -m src.seed"
    assert (plan["email_var"], plan["password_var"]) == ("ADMIN_EMAIL", "ADMIN_PASSWORD")


def test_no_plan_when_users_can_already_be_created_or_there_is_no_login():
    signup = {**SPEC, "paths": {**SPEC["paths"], "/auth/register": {"post": {"operationId": "register", "responses": {}}}}}
    assert bootstrap_plan({"db_schema.sql": SQL, "openapi.json": signup}) is None
    assert bootstrap_plan({"db_schema.sql": SQL}) is None
    assert bootstrap_plan({}) is None and bootstrap_plan(None) is None


@pytest.mark.skipif(not FLEET.is_dir(), reason="Fleet pack not present")
def test_fleet_gets_a_seed_item_before_the_entry_point_and_the_scaffold_declares_its_variables():
    plan = build_plan(FLEET)
    ids = [i.id for i in plan]
    seed = next(i for i in plan if i.id == "backend-seed-admin")
    assert seed.target_files == ["backend/app/seed.py"] and seed.tables == ["users"]
    assert "`administrator`" in seed.file_specs["backend/app/seed.py"] and "ADMIN_EMAIL" in seed.file_specs["backend/app/seed.py"]
    assert ids.index("backend-seed-admin") < ids.index("backend-entry")
    files = {e["path"]: e["content"] for e in render_scaffold("fleet", load_pack(FLEET))}
    assert "ADMIN_EMAIL=admin@example.com" in files[".env.example"] and "\nADMIN_PASSWORD=\n" in files[".env.example"]
    readme = files["README.md"]
    assert "cd backend\ncp ../.env.example ../.env" in readme and "python -m app.seed" in readme
    assert "### First login" in readme and "## Open questions in the design" in readme


@pytest.mark.skipif(not (ECOM.is_dir() and AUTH.is_dir()), reason="packs not present")
def test_packs_that_can_create_users_are_unchanged():
    for pack in (ECOM, AUTH):
        assert all(i.id != "backend-seed-admin" for i in build_plan(pack))
        readme = {e["path"]: e["content"] for e in render_scaffold("x", load_pack(pack))}["README.md"]
        assert "First login" not in readme
