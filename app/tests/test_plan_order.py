"""Adaptive plan shape: dependencies-first order, entry points last, data-layer tables, and the
directory leaves that used to vanish.

Every work item is generated in its own LLM call, in plan order. The adaptive builder used to emit
items in structure-tree order, which puts the directory holding ``main.py`` FIRST — so the app
factory was written before any router existed and guessed its imports (the Fleet product's
``from app.routers import ...`` names a package that was never generated). These tests pin the
order that makes "import what already exists" possible, and the smaller holes found alongside it:

* models/repositories items carried ``tables=[]`` (tables only arrived via endpoints), so the
  files that ARE the schema were generated without it;
* ``/vehicle-types`` "touched" ``vehicles`` too (substring match);
* a ``migrations/versions/`` directory leaf was dropped silently, so no migration was generated;
* icon folders always got a storefront's icon set, and font folders got SVGs named like fonts.

The legacy rich-mapping builders are untouched; their output is pinned elsewhere
(``test_design_pack_rich_mapping``, ``test_plan_builder_structure_shapes``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import WorkItem
from app.services.design_pack import DetectedFile
from app.services.plan_builder import (
    _INITIAL_MIGRATION_SPEC,
    _backend_layer,
    _expand_asset_leaf,
    _frontend_layer,
    _functional_req_ids,
    _order_and_shape,
    _synthesized_dir_leaves,
    _tables_for_endpoint,
    build_plan,
)


def _item(item_id: str, files: list[str], specs: dict[str, str] | None = None, **kw) -> WorkItem:
    return WorkItem(id=item_id, target_files=files, file_specs=specs or {}, **kw)


def _ids(items: list[WorkItem]) -> list[str]:
    return [i.id for i in items]


# --------------------------------------------------------------- backend order

def test_backend_items_are_ordered_dependencies_first_with_the_entry_point_last():
    # Tree order, as a structure file lists it: the app package (holding main.py) comes first.
    items = [
        _item("backend-root", ["backend/app/__init__.py", "backend/app/main.py", "backend/app/config.py",
                               "backend/app/database.py"]),
        _item("backend-modules-depots", ["backend/app/modules/depots/__init__.py",
                                         "backend/app/modules/depots/router.py",
                                         "backend/app/modules/depots/service.py",
                                         "backend/app/modules/depots/schemas.py"],
              endpoints=["GET /depots"], requirement_ids=["REQ-1"]),
        _item("backend-middleware", ["backend/app/middleware/__init__.py",
                                     "backend/app/middleware/error_handlers.py"]),
        _item("backend-models", ["backend/app/models/__init__.py", "backend/app/models/depot.py"]),
        _item("backend-utils", ["backend/app/utils/__init__.py", "backend/app/utils/security.py"]),
        _item("backend-manifest", ["backend/requirements.txt", "backend/Dockerfile"]),
    ]
    out = _order_and_shape(items, "backend", ["depots"])
    assert _ids(out) == [
        "backend-root", "backend-utils", "backend-models", "backend-middleware",
        "backend-modules-depots", "backend-entry", "backend-manifest",
    ]
    entry = next(i for i in out if i.id == "backend-entry")
    assert entry.target_files == ["backend/app/main.py"]
    root = out[0]
    assert "backend/app/main.py" not in root.target_files
    assert root.target_files == ["backend/app/__init__.py", "backend/app/config.py", "backend/app/database.py"]


def test_a_feature_module_stays_one_item_and_keeps_its_metadata():
    items = [_item("backend-modules-auth", ["backend/app/modules/auth/__init__.py",
                                            "backend/app/modules/auth/router.py",
                                            "backend/app/modules/auth/service.py",
                                            "backend/app/modules/auth/schemas.py"],
                   endpoints=["POST /auth/login"], requirement_ids=["REQ-7"], tables=["users"])]
    (only,) = _order_and_shape(items, "backend", ["users"])
    assert only.id == "backend-modules-auth"
    assert len(only.target_files) == 4
    assert only.endpoints == ["POST /auth/login"] and only.requirement_ids == ["REQ-7"]


def test_a_middleware_file_named_like_a_handler_is_not_a_module():
    # "error_handlers" contains "handler"; judged by name it would drag middleware after the
    # modules that import it.
    assert _backend_layer("backend/app/middleware/error_handlers.py", "") == "middleware"


def test_a_mixed_directory_is_split_and_the_split_part_carries_no_borrowed_metadata():
    items = [_item("backend-root", ["backend/app/config.py", "backend/app/dependencies.py"],
                   endpoints=["GET /health"], requirement_ids=["REQ-1"])]
    out = _order_and_shape(items, "backend")
    assert _ids(out) == ["backend-root", "backend-root-middleware"]
    assert out[0].endpoints == ["GET /health"]
    assert out[1].target_files == ["backend/app/dependencies.py"]
    assert out[1].endpoints == [] and out[1].requirement_ids == []


def test_an_entry_point_is_recognised_by_its_description_too():
    spec = {"backend/src/bootstrap.ts": "Application factory: registers middleware and mounts all routers"}
    out = _order_and_shape([_item("backend-src", ["backend/src/bootstrap.ts", "backend/src/config.ts"], spec)],
                           "backend")
    assert _ids(out) == ["backend-src", "backend-entry"]
    assert out[1].file_specs == spec


def test_a_loose_root_file_is_tool_configuration():
    assert _backend_layer("backend/knexfile.js", "Knex configuration per environment") == "config"
    assert _backend_layer("backend/ormconfig.ts", "") == "config"


def test_migrations_and_seeds_run_after_the_modules_but_before_the_entry_point():
    items = [
        _item("backend-db", ["backend/src/db/client.js", "backend/src/db/migrations/001_create_users.js",
                             "backend/src/db/seeds/01_roles.js"]),
        _item("backend-modules-users", ["backend/src/modules/users/users.routes.js",
                                        "backend/src/modules/users/users.controller.js"]),
        _item("backend-src", ["backend/src/app.js"]),
    ]
    out = _order_and_shape(items, "backend", ["users", "roles"])
    assert _ids(out) == ["backend-db", "backend-modules-users", "backend-db-migrations", "backend-entry"]
    migrations = out[2]
    assert migrations.target_files == ["backend/src/db/migrations/001_create_users.js",
                                       "backend/src/db/seeds/01_roles.js"]


# --------------------------------------------------------------- data-layer tables

def test_models_and_repositories_get_their_own_table_by_file_stem():
    items = [
        _item("backend-models", ["backend/app/models/vehicle.py", "backend/app/models/vehicle_type.py"]),
        _item("backend-repositories", ["backend/src/db/repositories/users.repository.js"]),
    ]
    out = _order_and_shape(items, "backend", ["vehicles", "vehicle_types", "users"])
    by_id = {i.id: i for i in out}
    assert by_id["backend-models"].tables == ["vehicle_types", "vehicles"]
    assert by_id["backend-repositories"].tables == ["users"]


def test_a_data_layer_item_matching_no_table_gets_all_of_them():
    # An __init__ that re-exports every model, or a base class: it needs the whole schema.
    out = _order_and_shape([_item("backend-models", ["backend/app/models/__init__.py",
                                                     "backend/app/models/base.py"])],
                           "backend", ["vehicles", "users"])
    assert out[0].tables == ["users", "vehicles"]


def test_migrations_always_get_every_table():
    out = _order_and_shape([_item("backend-migrations", ["backend/alembic/env.py",
                                                         "backend/alembic/versions/0001_initial.py"])],
                           "backend", ["vehicles", "users"])
    assert out[0].tables == ["users", "vehicles"]


def test_without_entities_no_tables_are_invented():
    out = _order_and_shape([_item("backend-models", ["backend/app/models/depot.py"])], "backend", [])
    assert out[0].tables == []


def test_an_endpoint_touches_the_table_its_path_names_not_every_table_containing_the_word():
    entities = ["vehicles", "vehicle_types", "users"]
    assert _tables_for_endpoint("/vehicle-types/{id}", "getVehicleType", entities) == ["vehicle_types"]
    assert _tables_for_endpoint("/vehicles/{id}/readings", "listReadings", entities) == ["vehicles"]


def test_an_endpoint_whose_path_names_no_table_falls_back_to_its_operation_id():
    assert _tables_for_endpoint("/auth/me", "getCurrentUser", ["users", "vehicles"]) == ["users"]
    assert _tables_for_endpoint("/health", "healthCheck", ["users"]) == []


# --------------------------------------------------------------- frontend order

def test_frontend_items_run_types_to_pages_to_router_with_app_and_main_last():
    items = [
        _item("frontend-src", ["frontend/src/main.tsx", "frontend/src/App.tsx"]),
        _item("frontend-router", ["frontend/src/router/AppRouter.tsx"]),
        _item("frontend-pages", ["frontend/src/pages/DashboardPage.tsx"]),
        _item("frontend-hooks", ["frontend/src/hooks/useAuth.ts"]),
        _item("frontend-api", ["frontend/src/api/client.ts"]),
        _item("frontend-types", ["frontend/src/types/index.ts"]),
        _item("frontend-styles", ["frontend/src/styles/tokens.css"]),
        _item("frontend-manifest", ["frontend/package.json", "frontend/vite.config.ts"]),
    ]
    out = _order_and_shape(items, "frontend")
    assert _ids(out) == [
        "frontend-styles", "frontend-types", "frontend-api", "frontend-hooks", "frontend-pages",
        "frontend-router", "frontend-entry", "frontend-manifest",
    ]
    assert out[-2].target_files == ["frontend/src/main.tsx", "frontend/src/App.tsx"]


def test_frontend_layers_by_shape():
    assert _frontend_layer("frontend/src/components/ui/Button.tsx", "") == "components-ui"
    assert _frontend_layer("frontend/src/components/StatusBadge.tsx", "") == "components"
    assert _frontend_layer("frontend/src/context/AuthContext.tsx", "") == "state"
    assert _frontend_layer("frontend/src/features/useVehicles.ts", "") == "hooks"


def test_ordering_is_stable_within_a_layer():
    items = [_item(f"backend-modules-{n}", [f"backend/app/modules/{n}/router.py"]) for n in ("b", "a", "c")]
    assert _ids(_order_and_shape(items, "backend")) == ["backend-modules-b", "backend-modules-a",
                                                        "backend-modules-c"]


# --------------------------------------------------------------- directory leaves

def test_an_alembic_versions_folder_becomes_an_initial_migration():
    tree = {"backend/": {"alembic/": {"env.py": "Alembic environment", "versions/": "Migration scripts"},
                         "alembic.ini": "Alembic config"}}
    synthesized, dropped = _synthesized_dir_leaves(tree)
    assert [p for p, _ in synthesized] == ["backend/alembic/versions/0001_initial.py"]
    assert synthesized[0][1] == _INITIAL_MIGRATION_SPEC
    assert dropped == []


def test_a_versions_folder_with_no_alembic_beside_it_is_reported_not_guessed():
    synthesized, dropped = _synthesized_dir_leaves({"backend/": {"docs/": {"versions/": "Old API versions"}}})
    assert synthesized == []
    assert dropped and dropped[0].startswith("backend/docs/versions/")


def test_asset_folders_are_not_reported_as_dropped():
    _, dropped = _synthesized_dir_leaves({"frontend/": {"src/": {"assets/": {"icons/": "UI icons"}}}})
    assert dropped == []


# --------------------------------------------------------------- assets

def test_an_icon_folder_uses_the_icons_its_description_names():
    paths = [p for p, _ in _expand_asset_leaf("src/assets/icons/", "Status icon SVGs (success, warning, error, info)")]
    assert paths == ["src/assets/icons/success.svg", "src/assets/icons/warning.svg",
                     "src/assets/icons/error.svg", "src/assets/icons/info.svg"]


def test_a_font_folder_produces_no_files():
    assert _expand_asset_leaf("src/assets/fonts/", "Inter font files (woff2)") == []


# --------------------------------------------------------------- traceability

def _req_roles(obj) -> dict:
    return {"requirements": [DetectedFile(path=None, role="requirements", obj=obj)]}  # type: ignore[arg-type]


def test_functional_requirement_ids_are_read_from_either_shape():
    assert _functional_req_ids(_req_roles({"functional": [{"id": "REQ-2"}, {"id": "REQ-1"}, {"id": "REQ-2"}],
                                           "business_rules": [{"id": "BR-1"}]})) == ["REQ-2", "REQ-1"]
    assert _functional_req_ids(_req_roles([{"id": "FR-01", "text": "..."}, {"text": "no id"}])) == ["FR-01"]
    assert _functional_req_ids({}) == []


# --------------------------------------------------------------- on-disk pack

_RESOURCES = Path(__file__).resolve().parents[2] / "fixtures" / "resources"


@pytest.mark.skipif(not _RESOURCES.is_dir(), reason="fixtures/resources pack not present")
def test_resources_pack_generates_each_sides_entry_point_after_everything_it_imports():
    plan = build_plan(_RESOURCES)
    for side in ("backend", "frontend"):
        ids = [i.id for i in plan if i.id.startswith(side + "-")]
        assert f"{side}-entry" in ids, f"{side}: no entry item"
        entry_at = ids.index(f"{side}-entry")
        modules = [n for n, i in enumerate(ids) if "-modules-" in i or i.endswith("-pages")]
        assert modules and max(modules) < entry_at, f"{side}: a module is generated after the entry point"
    repos = [i for i in plan if i.id.endswith("-repositories")]
    assert repos and all(i.tables for i in repos), "repository items must carry their tables"
