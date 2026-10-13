"""The naming contract built from the WHOLE pack, and the module map built from the plan.

The contract used to look artifacts up by filename (``schema.sql``, a Mongo ``db_schema.json``,
``api-mapping.csv``). The current Design-team format ships ``db_schema.sql``, a relational
``db_schema.json`` and an OpenAPI YAML, so the Fleet pack's contract had ZERO entities and its
endpoints were bare ``GET /x`` lines: the "use these exact names" block bound nothing. These tests
pin the replacement: shape-based input, the canonical facts preflight settled, endpoint lines that
carry the names files must agree on, and the module map that replaces import guessing (the Fleet
product's ``main.py`` imported an ``app.routers`` package nobody generated).

The original extractor tests still run unchanged in ``test_naming_contract.py`` /
``test_naming_contract_injection.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.code_generator import CodeGeneratorAgent
from app.models import WorkItem
from app.services import naming_contract as nc
from app.services.naming_contract import (
    _singularize,
    _snake,
    build_module_map,
    build_naming_contract,
    build_naming_contract_from_package,
)

_REPO = Path(__file__).resolve().parents[2]
FLEET = _REPO / "uploads" / "v1"
RESOURCES = _REPO / "fixtures" / "resources"

SQL = """CREATE TABLE users (
    id uuid NOT NULL,
    email varchar(320) NOT NULL,
    role varchar(30) NOT NULL,
    PRIMARY KEY (id),
    CHECK (role IN ('admin', 'clerk'))
);
CREATE TABLE order_categories (
    id uuid NOT NULL,
    status varchar(20) NOT NULL CHECK (status IN ('open', 'closed')),
    PRIMARY KEY (id)
);
"""

OPENAPI_YAML = """openapi: 3.0.3
info: {title: Shop, version: "1"}
servers:
  - url: https://api.example.com/api/v2
paths:
  /auth/login:
    post:
      operationId: login
      security: []
      requestBody:
        content:
          application/json:
            schema: {$ref: '#/components/schemas/LoginRequest'}
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema: {$ref: '#/components/schemas/Token'}
  /order-categories:
    get:
      operationId: listOrderCategories
      description: "Allowed roles: Admin, Clerk."
      security: [{bearerAuth: []}]
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema: {type: array, items: {$ref: '#/components/schemas/OrderCategory'}}
    post:
      operationId: createOrderCategory
      description: "Allowed roles: Admin."
      security: [{bearerAuth: []}]
      requestBody:
        content:
          application/json:
            schema: {$ref: '#/components/schemas/OrderCategoryCreate'}
      responses:
        "201":
          description: created
          content:
            application/json:
              schema: {$ref: '#/components/schemas/OrderCategory'}
  /order-categories/{id}:
    delete:
      operationId: deleteOrderCategory
      security: [{bearerAuth: []}]
      responses:
        "204": {description: gone}
components:
  schemas:
    Error:
      type: object
      required: [code, message]
      properties: {code: {type: string}, message: {type: string}}
"""


def _pack(backend_files: dict[str, str] | None = None) -> dict:
    tree = {"backend/": {"app/": backend_files or {"__init__.py": "Package initialiser",
                                                    "main.py": "FastAPI application factory"}}}
    return {
        "db_schema.sql": SQL,
        "api/spec.yaml": OPENAPI_YAML,                 # named nothing like "openapi.yaml"
        ".env.example": "DATABASE_URL=postgresql://localhost/shop\nJWT_SECRET=change-me\nPORT=8000\n",
        "structure/backend.json": {"tree": tree},
    }


# --------------------------------------------------------------- the contract

def test_entities_come_from_a_schema_whatever_it_is_called_with_their_allowed_values():
    contract = build_naming_contract_from_package(_pack())
    assert "- `User` <- `users`: id, email, role (admin|clerk)" in contract
    assert "- `OrderCategory` <- `order_categories`: id, status (open|closed)" in contract


def test_every_operation_carries_the_names_files_must_agree_on():
    contract = build_naming_contract_from_package(_pack())
    lines = contract.splitlines()
    assert "- POST /auth/login -> login | body LoginRequest | returns Token | public" in lines
    assert ("- GET /order-categories -> listOrderCategories | handler list_order_categories | "
            "returns OrderCategory[] | roles: admin, clerk") in lines
    assert ("- POST /order-categories -> createOrderCategory | handler create_order_category | "
            "body OrderCategoryCreate | returns 201 OrderCategory | roles: admin") in lines
    assert ("- DELETE /order-categories/{id} -> deleteOrderCategory | handler delete_order_category | "
            "returns 204 (no body) | any signed-in user") in lines


def test_canonical_values_lead_the_contract():
    contract = build_naming_contract_from_package(_pack())
    assert contract.startswith("NAMING CONTRACT")
    facts = contract.split("Canonical values", 1)[1].split("\nEntities", 1)[0]
    assert "API base path `/api/v2`" in facts
    assert "`admin`, `clerk`" in facts and "admin = Admin" in facts        # stored values vs labels
    assert "`Error` {code, message}" in facts
    assert "DATABASE_URL, JWT_SECRET, PORT." in facts
    assert contract.index("Canonical values") < contract.index("Entities") < contract.index("Endpoints")


def test_a_javascript_backend_gets_no_snake_case_handler_names():
    contract = build_naming_contract_from_package(_pack({"server.js": "Express entry point"}))
    assert "| handler " not in contract                    # the operationId IS the handler name
    assert "- GET /order-categories -> listOrderCategories | returns OrderCategory[]" in contract


def test_access_is_not_claimed_when_the_spec_never_mentions_security():
    pkg = _pack()
    pkg["api/spec.yaml"] = OPENAPI_YAML.replace("      security: []\n", "").replace(
        "      security: [{bearerAuth: []}]\n", "")
    contract = build_naming_contract_from_package(pkg)
    assert "public" not in contract and "signed-in" not in contract
    assert "roles: admin, clerk" in contract                    # stated roles still are


def test_disk_and_in_memory_contracts_are_identical(tmp_path: Path):
    for name, value in _pack().items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    assert build_naming_contract(tmp_path) == build_naming_contract_from_package(_pack())


def test_the_contract_is_ascii():
    assert build_naming_contract_from_package(_pack()).isascii()


def test_an_oversized_contract_is_cut_on_a_line_boundary(monkeypatch):
    monkeypatch.setattr(nc, "_CONTRACT_MAX_CHARS", 900)
    contract = build_naming_contract_from_package(_pack())
    assert contract.endswith("\n- ... (truncated)")
    body = contract[: -len("\n- ... (truncated)")]
    assert len(body) <= 900
    assert all(line == "" or line.startswith(("-", "NAMING", "Canonical", "Entities", "Endpoints"))
               for line in body.splitlines())


def test_facts_alone_are_not_a_contract():
    assert build_naming_contract_from_package({".env.example": "DATABASE_URL=x\n"}) == ""


@pytest.mark.parametrize("word,singular", [
    ("users", "User"), ("categories", "Category"), ("addresses", "Address"), ("boxes", "Box"),
    ("status", "Status"), ("t", "T"), ("data", "Data"),
])
def test_model_class_names_are_singular(word, singular):
    assert nc._pascal(_singularize(word)) == singular


@pytest.mark.parametrize("op,snake", [
    ("getDepotDashboard", "get_depot_dashboard"), ("getOBDData", "get_obd_data"),
    ("login", "login"), ("list_items", "list_items"),
])
def test_snake_case_handler_names(op, snake):
    assert _snake(op) == snake


# --------------------------------------------------------------- the module map

def _items(*groups: list[str]) -> list[WorkItem]:
    return [WorkItem(id=f"item-{n}", target_files=files) for n, files in enumerate(groups)]


def test_python_modules_are_named_from_the_real_import_root():
    plan = _items(
        ["backend/app/__init__.py", "backend/app/config.py", "backend/app/main.py"],
        ["backend/app/modules/auth/__init__.py", "backend/app/modules/auth/router.py"],  # modules/ has no __init__
        ["backend/app/models/__init__.py", "backend/app/models/user.py"],
    )
    text = build_module_map(plan)
    assert text.startswith("MODULE MAP")
    assert "import root `backend/`" in text
    assert "- app: config, main" in text
    assert "- app.modules.auth: router" in text            # a namespace package, not a root of its own
    assert "- app.models: user" in text


def test_a_package_with_only_an_init_is_listed_as_a_package():
    text = build_module_map(_items(["backend/app/__init__.py", "backend/app/models/__init__.py",
                                    "backend/app/config.py"]))
    assert "- app.models (package)" in text


def test_python_without_any_init_uses_the_side_folder_as_root():
    text = build_module_map(_items(["backend/app/main.py", "backend/app/db/session.py"]))
    assert "import root `backend/`" in text
    assert "- app.db: session" in text


def test_javascript_files_are_grouped_by_folder():
    text = build_module_map(_items(["frontend/src/api/client.ts", "frontend/src/api/orders.ts",
                                    "frontend/src/App.tsx"]))
    assert "paths relative to `frontend/`" in text
    assert "- src/api: client.ts, orders.ts" in text
    assert "- src: App.tsx" in text


def test_only_importable_source_files_are_listed():
    text = build_module_map(_items([
        "backend/app/__init__.py", "backend/app/main.py",
        "backend/alembic/env.py", "backend/alembic/versions/0001_initial.py",
        "backend/tests/test_auth.py", "backend/requirements.txt", "backend/Dockerfile",
        "frontend/vite.config.ts", "frontend/src/App.test.tsx", "frontend/package.json",
        "frontend/src/main.tsx",
    ]))
    for absent in ("alembic", "0001_initial", "test_auth", "requirements", "Dockerfile", "vite.config",
                   "App.test", "package.json"):
        assert absent not in text, absent
    assert "- app: main" in text and "- src: main.tsx" in text


def test_an_item_sees_only_the_side_it_writes_into():
    plan = _items(["backend/app/__init__.py", "backend/app/main.py"], ["frontend/src/App.tsx"])
    backend_view = build_module_map(plan, for_item=plan[0])
    assert "app: main" in backend_view and "App.tsx" not in backend_view
    frontend_view = build_module_map(plan, for_item=plan[1])
    assert "App.tsx" in frontend_view and "app: main" not in frontend_view


def test_work_items_may_arrive_as_dicts():
    assert "- src: App.tsx" in build_module_map([{"id": "x", "target_files": ["frontend/src/App.tsx"]}])


def test_nothing_importable_means_no_module_map():
    assert build_module_map([]) == ""
    assert build_module_map(None) == ""
    assert build_module_map(_items(["backend/requirements.txt"])) == ""


# --------------------------------------------------------------- injection

def test_the_module_map_follows_the_contract_in_every_generation_context():
    plan = _items(["backend/app/__init__.py", "backend/app/main.py"],
                  ["backend/app/modules/auth/__init__.py", "backend/app/modules/auth/router.py"])
    context, sections = CodeGeneratorAgent()._assemble_context(plan[0], _pack(), [], work_items=plan)
    assert sections[:2] == ["Naming contract", "Module map"]
    assert context.index("NAMING CONTRACT") == 0
    assert "app.modules.auth: router" in context      # the router main.py must import, before it exists


def test_no_module_map_without_a_plan():
    _context, sections = CodeGeneratorAgent()._assemble_context(_items(["backend/app/main.py"])[0], _pack())
    assert "Module map" not in sections


# --------------------------------------------------------------- the real packs

@pytest.mark.skipif(not FLEET.is_dir(), reason="Fleet pack (uploads/v1) not present")
def test_fleet_contract_binds_entities_endpoints_and_facts():
    contract = build_naming_contract(FLEET)
    assert contract.count("\n- `") >= 7                                  # every table
    assert "role (administrator|manager|technician)" in contract
    assert "-> getDepotDashboard | handler get_depot_dashboard" in contract
    assert "`require_role` lives in backend/app/middleware/rbac.py" in contract
    assert "API base path `/v1`" in contract


@pytest.mark.skipif(not RESOURCES.is_dir(), reason="fixtures/resources pack not present")
def test_ecommerce_contract_fits_its_budget_without_truncation():
    contract = build_naming_contract(RESOURCES)
    assert "(truncated)" not in contract
    assert contract.count("\n- `") >= 20
    assert len([ln for ln in contract.splitlines() if ln.startswith(("- GET /", "- POST /", "- PUT /",
                                                                      "- PATCH /", "- DELETE /"))]) >= 90
