"""The design-pack slices each generation call is built from (``app/services/pack_context.py``).

Context used to be found by filename and cut with text filters: an OpenAPI YAML contributed only
the lines containing a cited path, the mockup only the lines naming a screen, the validation rules
only the groups whose KEY contained the endpoint string. On the current pack format that left
backend items with no conventions, no tables and no schemas. These tests pin what replaced it, on
small synthetic packs, so each rule is visible on its own.
"""

from __future__ import annotations

from app.services import pack_context as pc
from app.services.pack_index import PackIndex

SPEC = {
    "openapi": "3.0.3",
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/auth/login": {"post": {
            "operationId": "login", "security": [], "summary": "Log in",
            "requestBody": {"required": True, "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/LoginRequest"}}}},
            "responses": {"200": {"description": "ok", "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/Token"}}}},
                "401": {"$ref": "#/components/responses/Unauthorized"}},
        }},
        "/vehicle-types": {"get": {
            "operationId": "listVehicleTypes", "tags": ["vehicle-types"], "security": [{"bearerAuth": []}],
            "parameters": [{"name": "page", "in": "query", "schema": {"type": "integer", "default": 1}}],
            "responses": {"200": {"description": "ok", "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/VehicleTypeList"}}}}},
        }},
        "/vehicles/{vehicleId}": {
            "parameters": [{"name": "vehicleId", "in": "path", "required": True, "schema": {"type": "string"}}],
            "get": {"operationId": "getVehicle", "security": [{"bearerAuth": []}],
                    "responses": {"200": {"description": "ok", "content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/Vehicle"}}}}}},
            "put": {"operationId": "updateVehicle", "security": [{"bearerAuth": []}],
                    "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/VehicleUpdate"}}}},
                    "responses": {"200": {"description": "ok"}}},
        },
        "/work-orders": {"get": {"operationId": "listWorkOrders", "responses": {"200": {"description": "ok"}}}},
        "/work-orders/my": {"get": {"operationId": "listMyWorkOrders", "responses": {"200": {"description": "ok"}}}},
    },
    "components": {
        "schemas": {
            "LoginRequest": {"type": "object", "properties": {"email": {"type": "string"}}},
            "Token": {"type": "object", "properties": {"access_token": {"type": "string"}}},
            "VehicleTypeList": {"type": "object", "properties": {"items": {"type": "array", "items": {
                "$ref": "#/components/schemas/VehicleType"}}}},
            "VehicleType": {"type": "object", "properties": {"name": {"type": "string"}}},
            "Vehicle": {"type": "object", "properties": {"id": {"type": "string"}}},
            "VehicleUpdate": {"type": "object", "properties": {"make": {"type": "string"}}},
            "Error": {"type": "object", "properties": {"code": {"type": "string"}}},
        },
        "responses": {"Unauthorized": {"description": "no", "content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Error"}}}}},
        "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
    },
}

SQL = """CREATE TYPE vehicle_status AS ENUM ('active', 'retired');

-- Vehicles in the fleet.
-- One row per registered vehicle.
CREATE TABLE vehicles (

    id uuid NOT NULL,

    status vehicle_status NOT NULL DEFAULT 'active',

    PRIMARY KEY (id)

);
CREATE INDEX idx_vehicles_status ON vehicles (status);
ALTER TABLE vehicles ADD CONSTRAINT fk_x FOREIGN KEY (id) REFERENCES other (id);
CREATE TABLE other (id uuid PRIMARY KEY);
"""


def _index(**extra) -> PackIndex:
    return PackIndex({"openapi.json": SPEC, "db_schema.sql": SQL, **extra})


# --------------------------------------------------------------- API

def test_cited_operations_match_whatever_the_path_parameter_is_called():
    ix = _index()
    ops = pc.operations_for(ix, ["GET /vehicles/{id}", "POST /auth/login"])
    assert [(m, p) for m, p, _ in ops] == [("POST", "/auth/login"), ("GET", "/vehicles/{vehicleId}")]
    assert len(pc.operations_for(ix, ["/vehicles/{vehicleId}"])) == 2          # a bare path: every method


def test_the_api_slice_is_parsed_and_carries_every_schema_it_references_transitively():
    ix = _index()
    text = pc.api_section(ix, pc.operations_for(ix, ["POST /auth/login", "GET /vehicle-types"]))
    assert "### POST /auth/login  (operationId: login)" in text
    assert "security: none (public)" in text
    assert "request body: LoginRequest (required)" in text
    assert "responses: 200 Token; 401 Error" in text                       # through components/responses
    assert "parameters: page (query, integer, default 1)" in text
    for schema in ("LoginRequest:", "Token:", "VehicleTypeList:", "VehicleType:", "Error:"):
        assert f"\n{schema}" in text, schema                                 # VehicleType only via a nested $ref
    assert "securitySchemes/bearerAuth" in text
    assert "Vehicle:" not in text.replace("VehicleType", "")                 # nothing uncited


def test_path_level_parameters_are_part_of_the_operation():
    ix = _index()
    assert "parameters: vehicleId (path, string, required)" in pc.api_section(ix, pc.operations_for(ix, ["GET /vehicles/{x}"]))


def test_api_basics_carry_server_security_and_the_error_body():
    text = pc.api_basics(_index(), "Error")
    assert "https://api.example.com/v1" in text and "bearerAuth" in text and "Error:" in text


def test_a_frontend_file_gets_the_operations_of_the_resource_it_is_named_after():
    ix = _index()

    def ops(path: str) -> list[str]:
        return [f"{m} {p}" for m, p, _ in pc.operations_for_file(ix, path)]

    assert ops("src/services/vehicleTypesService.ts") == ["GET /vehicle-types"]   # not /vehicles
    assert ops("src/types/vehicle.ts") == ["GET /vehicles/{vehicleId}", "PUT /vehicles/{vehicleId}"]
    assert ops("src/hooks/useMyWorkOrders.ts")[0] == "GET /work-orders/my"        # the closer one first
    assert ops("src/services/authService.ts") == ["POST /auth/login"]
    assert ops("src/services/apiClient.ts") == []
    assert ops("src/components/LoginForm.tsx") == []                               # no /login resource


# --------------------------------------------------------------- DB

def test_the_db_slice_is_the_packs_own_ddl_with_its_satellite_statements():
    text = pc.db_section(_index(), ["vehicles"])
    assert text.startswith("CREATE TYPE vehicle_status AS ENUM")                 # the type the table uses
    assert "-- Vehicles in the fleet.\n-- One row per registered vehicle.\nCREATE TABLE vehicles (" in text
    assert "CREATE INDEX idx_vehicles_status ON vehicles (status);" in text
    assert "ALTER TABLE vehicles ADD CONSTRAINT fk_x" in text
    assert "\n    id uuid NOT NULL,\n    status vehicle_status" in text            # blank lines gone, indent kept
    assert "CREATE TABLE other" not in text


def test_a_json_only_schema_is_rendered_from_the_normalized_tables():
    ix = PackIndex({"db_schema.json": {"tables": [{"name": "users", "columns": [
        {"name": "id", "type": "uuid", "nullable": False}, {"name": "role", "type": "text", "enum": ["admin", "clerk"]}],
        "primary_key": ["id"]}]}})
    text = pc.db_section(ix, ["users"])
    assert text.splitlines()[0] == "TABLE users"
    assert "  id uuid NOT NULL" in text and "role text IN ('admin', 'clerk')" in text and "PRIMARY KEY (id)" in text


def test_no_tables_no_slice():
    assert pc.db_section(_index(), []) == "" and pc.db_section(_index(), ["missing"]) == ""


# --------------------------------------------------------------- validation rules

RULES = {
    "login": {"email": {"message": "Enter a valid email address."}},                 # by operationId
    "VehicleUpdate": {"make": {"message": "Make is required."}},                     # by request schema
    "GET /vehicle-types": {"page": {"message": "Page must be positive."}},           # by METHOD /path
    "passwordPolicy": {"rules": [{"message": "Password must be at least 8 characters."}]},  # bound to nothing
    "Vehicle": {"id": {"message": "bad id"}},                                        # a schema no op takes
}


def test_rule_groups_bind_by_operation_id_request_schema_or_endpoint():
    ix = _index(**{"validation-rules.json": RULES})
    login = pc.validation_section(ix, pc.operations_for(ix, ["POST /auth/login"]))
    assert "Enter a valid email address." in login and "Make is required." not in login
    update = pc.validation_section(ix, pc.operations_for(ix, ["PUT /vehicles/{vehicleId}"]))
    assert "Make is required." in update
    listing = pc.validation_section(ix, pc.operations_for(ix, ["GET /vehicle-types"]))
    assert "Page must be positive." in listing


def test_groups_bound_to_no_operation_are_shared_by_every_item():
    shared = pc.validation_shared(_index(**{"validation-rules.json": RULES}))
    assert "passwordPolicy" in shared
    for bound in ("login", "VehicleUpdate", "GET /vehicle-types", '"Vehicle"'):
        assert bound not in shared, bound


def test_rules_keyed_by_endpoint_bind_even_without_a_spec():
    ix = PackIndex({"validation-rules.json": {"POST /login": {"password": "Password is required."}}})
    assert "Password is required." in pc.validation_section(ix, [], ["POST /login"])
    assert pc.validation_shared(ix) == ""


# --------------------------------------------------------------- requirements, payloads

def test_requirements_carry_their_text_and_rules_are_pack_wide():
    ix = PackIndex({"requirements.json": {
        "functional": [{"id": "REQ-1", "text": "Show the dashboard.", "acceptance_criteria": ["lists due vehicles"]},
                       {"id": "REQ-2", "text": "Other."}],
        "non_functional": [{"id": "NFR-1", "text": "Hash passwords."}],
        "business_rules": [{"id": "BR-1", "text": "Heavy trucks have their own schedule."}],
        "constraints": ["PostgreSQL only"],
    }})
    assert pc.requirements_section(ix, ["REQ-1"]) == "- REQ-1: Show the dashboard.\n  - acceptance: lists due vehicles"
    rules = pc.rules_section(ix)
    assert "- BR-1: Heavy trucks have their own schedule." in rules and "- NFR-1: Hash passwords." in rules
    assert "- PostgreSQL only" in rules


def test_sample_payloads_are_picked_by_operation():
    ix = _index(**{"sample-payloads.json": {
        "login": {"method": "POST", "path": "/auth/login", "request": {"email": "a@b.c"}},
        "other": {"method": "GET", "path": "/vehicles/{vehicleId}", "responses": {"200": {"id": "x"}}},
        "unrelated": {"method": "GET", "path": "/elsewhere"},
    }})
    text = pc.payloads_section(ix, pc.operations_for(ix, ["POST /auth/login", "GET /vehicles/{id}"]))
    assert '"login"' in text and '"other"' in text and "unrelated" not in text


# --------------------------------------------------------------- screens

def test_a_page_file_maps_to_the_screen_that_agrees_with_what_it_does():
    screens = ["Admin Catalogue Products", "Admin Create Product", "Admin Edit Product", "Admin Order List",
               "Vehicle List", "Vehicle Detail", "Depot Dashboard"]
    assert pc.screen_for_file("pages/AdminProductList.jsx", screens) == "Admin Catalogue Products"
    assert pc.screen_for_file("pages/AdminProductNew.jsx", screens) == "Admin Create Product"
    assert pc.screen_for_file("pages/AdminProductEdit.jsx", screens) == "Admin Edit Product"
    assert pc.screen_for_file("pages/VehicleDetailPage.tsx", screens) == "Vehicle Detail"
    assert pc.screen_for_file("pages/DashboardPage.tsx", screens) == "Depot Dashboard"
    assert pc.screen_for_file("pages/NotFound.jsx", screens) is None


def test_a_tie_goes_to_the_preferred_screen_or_to_nobody():
    screens = ["Order Summary", "Order Status"]
    assert pc.screen_for_file("pages/Order.tsx", screens) is None
    assert pc.screen_for_file("pages/Order.tsx", screens, preferred=["Order Status"]) == "Order Status"


MOCKUP = """<html><body>
<section id="screen-style-guide">
    <button class="btn">Go</button>
</section>
<section id="screen-admin-product-list">
    <div class="vehicle-card">
        <span>card</span>
    </div>
</section>
<section id="screen-login"><form></form></section>
</body></html>"""


def test_a_page_named_exactly_like_a_mockup_section_is_that_screen():
    ix = PackIndex({"mockup.html": MOCKUP})
    assert pc.own_screen(ix, "src/pages/AdminProductListPage.jsx") == "Admin Product List"
    assert pc.own_screen(ix, "src/pages/AdminProducts.jsx") is None


def test_every_screen_of_the_pack_is_a_candidate():
    ix = PackIndex({"mockup.html": MOCKUP, "api-to-ui-mapping.csv":
                    "screen,ui_field,direction,api_endpoint,api_field\nLogin,Email,send,POST /auth/login,email\n"
                    "Login,Pass,send,POST /auth/login,password\n"})
    assert pc.all_screens(ix) == ["Login", "Admin Product List"]                 # the style guide is not a screen


def test_mockup_slices_are_compacted_and_components_find_their_element():
    ix = PackIndex({"mockup.html": MOCKUP})
    section = pc.mockup_for_screen(ix, "Admin Product List")
    assert section.startswith('<section id="screen-admin-product-list">') and "\n    " not in section
    assert pc.style_guide_section(ix) == '<section id="screen-style-guide">\n<button class="btn">Go</button>\n</section>'
    assert pc.component_fragment(ix.mockup_html(), "src/components/VehicleCard.tsx") == (
        '<div class="vehicle-card">\n<span>card</span>\n</div>')
    assert pc.component_fragment(ix.mockup_html(), "src/components/Missing.tsx") == ""


# --------------------------------------------------------------- configuration, budgets

def test_config_files_are_recognised_by_name_or_folder():
    for path in ("backend/app/config.py", "backend/app/core/settings.py", "backend/knexfile.js",
                 "frontend/vite.config.ts", "frontend/.env.example", "backend/src/config/index.js"):
        assert pc.is_config_file(path), path
    assert not pc.is_config_file("backend/app/modules/auth/router.py")


def test_env_vars_show_examples_except_for_secrets():
    ix = PackIndex({"env-vars.json": {"groups": [{"name": "app", "vars": [
        {"key": "DATABASE_URL", "example": "postgresql://localhost/app", "required": True},
        {"key": "JWT_SECRET", "example": "s3cr3t", "secret": True, "comment": "signing key"}]}]}})
    text = pc.env_section(ix)
    assert "- DATABASE_URL (required) = postgresql://localhost/app" in text
    assert "- JWT_SECRET (secret)  # signing key" in text and "s3cr3t" not in text


def test_clip_cuts_on_a_line_boundary_and_says_so():
    text = "\n".join(f"line {n}" for n in range(100))
    clipped = pc.clip(text, 50)
    assert clipped.splitlines()[-1].startswith("... (") and "more characters not shown" in clipped
    assert all(line.startswith(("line ", "... (")) for line in clipped.splitlines())
    assert pc.clip("short", 50) == "short"


# --------------------------------------------------------------- the index's name hints

def test_a_role_no_artifact_has_by_shape_is_taken_from_a_name_hint():
    ix = PackIndex({"SKILL.md": "Use snake_case.", "openapi.yaml": {"paths": {"/x": {"get": {}}}}})
    assert ix.conventions_text() == "Use snake_case."
    assert ix.operations() == [("GET", "/x", {})]


def test_a_name_hint_never_overrides_a_role_found_by_shape():
    rules = "\n".join(f"- Always do thing {n}; never skip it." for n in range(10))
    ix = PackIndex({"guidelines.md": rules, "SKILL.md": "short"})
    assert ix.names("conventions") == ["guidelines.md"]
