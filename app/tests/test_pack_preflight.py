"""Pack preflight: a design pack's contradictions and gaps, settled before generation.

Each check is exercised on a small synthetic pack, then asserted against the real Fleet pack
(``uploads/v1``), whose contradictions caused real failures: roles compared as "Administrator"
while the DB stored "administrator", ``vehicle_class: "Heavy"`` against a ``heavy_truck`` CHECK,
non-hex example UUIDs, and a login with no way to create the first user. The hand-built, coherent
``uploads/authentication`` pack must come out clean — the checks must not cry wolf.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.pack_index import PackIndex
from app.services.pack_preflight import (
    GAP, INFO, NORMALIZED, normalized_sample_payloads, preflight_for, run_preflight,
)

_REPO = Path(__file__).resolve().parents[2]
FLEET = _REPO / "uploads" / "v1"
AUTH = _REPO / "uploads" / "authentication"

SQL = """CREATE TABLE users (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    email varchar(320) NOT NULL,
    password_hash varchar(255) NOT NULL,
    role varchar(30) NOT NULL,
    PRIMARY KEY (id),
    CHECK (role IN ('administrator', 'manager'))
);
CREATE TABLE items (
    id uuid NOT NULL,
    owner_id uuid,
    kind varchar(30) NOT NULL,
    PRIMARY KEY (id),
    CHECK (kind IN ('heavy_truck', 'light_commercial'))
);
ALTER TABLE items ADD CONSTRAINT fk_items_owner FOREIGN KEY (owner_id) REFERENCES users (id);
"""


def _spec(**extra_paths) -> dict:
    paths = {
        "/auth/login": {"post": {"operationId": "login", "description": "Public.",
                                 "responses": {"200": {"description": "ok", "content": {"application/json": {
                                     "example": {"access_token": _jwt({"sub": "x", "role": "Manager"})}}}}}}},
        "/items": {
            "get": {"operationId": "listItems", "description": "Allowed roles: Administrator, Manager.",
                    "responses": {"200": {"description": "ok"}}},
            "post": {"operationId": "createItem", "description": "Allowed roles: Administrator.",
                     "requestBody": {"content": {"application/json": {
                         "example": {"kind": "Heavy Truck", "owner_id": "u1v2w3x4-y5z6-7890-abcd-333333333333"}}}},
                     "responses": {"201": {"description": "ok"}}},
        },
        "/ingest": {"post": {"operationId": "ingest", "description": "Allowed roles: internal.",
                             "responses": {"200": {"description": "ok"}}}},
    }
    paths.update(extra_paths)
    return {
        "openapi": "3.1.0", "info": {"title": "t", "version": "1"},
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": paths,
        "components": {"schemas": {
            "Item": {"type": "object", "properties": {"id": {"type": "string", "format": "uuid"},
                                                       "owner_id": {"type": "string", "format": "uuid"},
                                                       "kind": {"type": "string", "enum": ["heavy_truck", "medium"]}}},
            "Error": {"type": "object", "required": ["code", "message"],
                      "properties": {"code": {"type": "string"}, "message": {"type": "string"}}},
        }},
    }


def _jwt(claims: dict) -> str:
    import base64
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.sig"


MAPPING = (
    "screen,ui_field,direction,api_endpoint,api_field\n"
    "Item Form,Kind,send,POST /items,kind\n"
    "Item Form,Assign owner,send,POST /items,owner_id\n"
    "Item Form,Owner,receive,GET /owners,data.name\n"
)
MOCKUP = ('<html><body><section id="screen-item-form"><select name="owner_id"><option value="1">A</option>'
          '</select><select name="kind"><option value="heavy_truck">H</option><option value="off_road">O</option>'
          "</select></section></body></html>")
CONVENTIONS = (
    "## Coding Guidelines\n"
    "- Always use require_role('admin') at the router level.\n"
    '- Return consistent error envelopes: {"detail": "<message>", "code": "<ERROR_CODE>"} for all errors.\n'
    "- Prefix all environment variables consumed by the backend with APP_ and document them.\n"
    "- Never log secrets.\n- Always hash passwords.\n- Never use raw SQL.\n- Always write tests.\n- Avoid globals.\n"
)
STRUCTURE = {"root/": {"backend/": {"app/": {
    "config.py": "Settings class reading environment variables DATABASE_URL, SECRET_KEY",
    "dependencies.py": "Shared callables: get_db session, get_current_user, require_role",
    "middleware/": {"rbac.py": "require_role dependency factory enforcing role checks"},
    "errors.py": "Maps INVALID_CREDENTIALS and RATE_LIMITED error codes to responses",
}}}}
ENV = {"groups": [{"name": "Core", "vars": [{"key": "DATABASE_URL", "example": ""},
                                            {"key": "JWT_SECRET_KEY", "example": ""},
                                            {"key": "PORT", "example": "8000"}]}]}
SAMPLES = {"createItem": {"method": "POST", "path": "/items",
                          "request": {"kind": "Heavy", "owner_id": "u1v2w3x4-y5z6-7890-abcd-333333333333"},
                          "responses": {"201": {"id": "g7h8i9j0-k1l2-3456-mnop-ef7890123456"}}},
           "listItems": {"method": "GET", "path": "/items", "responses": {"200": {"data": []}}}}


def _pack(**overrides) -> dict:
    pkg = {
        "openapi.json": _spec(), "db_schema.sql": SQL, "api-to-ui-mapping.csv": MAPPING,
        "mockup.html": MOCKUP, "skills.md": CONVENTIONS, "backend-structure.json": STRUCTURE,
        "env-vars.json": ENV, "sample-payloads.json": SAMPLES,
    }
    pkg.update(overrides)
    return pkg


def _by_check(result) -> dict[str, list]:
    out: dict[str, list] = {}
    for f in result.findings:
        out.setdefault(f.check, []).append(f)
    return out


@pytest.fixture(scope="module")
def synthetic():
    return run_preflight(PackIndex(_pack()))


def test_enum_drift_db_wins_and_lists_every_source(synthetic) -> None:
    drift = _by_check(synthetic)["enum_drift"]
    kind = next(f for f in drift if "`kind`" in f.title)
    assert kind.severity == NORMALIZED
    assert kind.canonical == {"items": ["heavy_truck", "light_commercial"]}
    for bad in ("Heavy Truck", "Heavy", "medium", "off_road"):          # openapi, samples, schema enum, mockup
        assert f"`{bad}`" in kind.detail
    assert "`Heavy Truck` → `heavy_truck`" in kind.detail
    assert synthetic.facts["enums"]["items.kind"] == ["heavy_truck", "light_commercial"]


def test_roles_canonical_from_db_with_every_other_spelling_reported(synthetic) -> None:
    roles = synthetic.facts["roles"]
    assert roles["values"] == ["administrator", "manager"]
    assert roles["display"] == {"administrator": "Administrator", "manager": "Manager"}
    vocab = _by_check(synthetic)["role_vocabulary"][0]
    for spelling in ("`Administrator`", "`Manager`", "`admin`"):          # openapi, sample JWT, conventions
        assert spelling in vocab.detail
    assert synthetic.facts["operation_roles"]["GET /items"] == ["administrator", "manager"]


def test_a_role_no_user_can_have_is_a_gap(synthetic) -> None:
    unknown = _by_check(synthetic)["unknown_role"][0]
    assert unknown.severity == GAP and "`internal`" in unknown.title and "POST /ingest" in unknown.detail


def test_malformed_example_uuids_are_reported_and_replaced_consistently() -> None:
    index = PackIndex(_pack())
    finding = _by_check(run_preflight(index))["malformed_example"][0]
    assert finding.title.startswith("2 example id(s)")
    fixed = normalized_sample_payloads(index)
    owner = fixed["createItem"]["request"]["owner_id"]
    import uuid
    uuid.UUID(owner)                                                    # now a valid UUID
    assert owner == normalized_sample_payloads(index)["createItem"]["request"]["owner_id"]   # deterministic
    assert SAMPLES["createItem"]["request"]["owner_id"].startswith("u1v2")   # the pack itself is untouched


def test_a_chosen_reference_without_a_list_endpoint_is_a_gap(synthetic) -> None:
    gap = _by_check(synthetic)["missing_list_endpoint"][0]
    assert gap.severity == GAP and "`users`" in gap.title and "`owner_id`" in gap.detail
    missing = _by_check(synthetic)["missing_endpoint"][0]
    assert "`GET /owners`" in missing.title                             # the UI calls a path the API lacks


def test_a_reference_sent_from_context_is_not_flagged() -> None:
    no_select = MOCKUP.replace('<select name="owner_id"><option value="1">A</option></select>', "")
    result = run_preflight(PackIndex(_pack(**{"mockup.html": no_select})))
    assert "missing_list_endpoint" not in _by_check(result)


def test_login_without_user_creation_needs_a_bootstrap(synthetic) -> None:
    gap = _by_check(synthetic)["bootstrap_needed"][0]
    assert gap.severity == GAP
    assert synthetic.facts["auth"] == {"login": "POST /auth/login", "user_table": "users",
                                       "user_creation": [], "bootstrap_needed": True}
    with_signup = _spec(**{"/users": {"post": {"operationId": "createUser", "responses": {}}}})
    result = run_preflight(PackIndex(_pack(**{"openapi.json": with_signup})))
    assert "bootstrap_needed" not in _by_check(result)


def test_env_names_checked_only_where_config_is_described(synthetic) -> None:
    stray = {f.title.split("`")[1]: f for f in _by_check(synthetic)["env_name_conflict"]}
    assert set(stray) == {"SECRET_KEY"}                    # not the error codes in errors.py
    assert stray["SECRET_KEY"].canonical == "JWT_SECRET_KEY"
    assert _by_check(synthetic)["env_prefix_rule"][0].title.startswith("Conventions require a `APP_` prefix")


def test_a_helper_described_twice_gets_one_owner(synthetic) -> None:
    dup = _by_check(synthetic)["duplicate_owner"][0]
    assert dup.canonical == {"symbol": "require_role", "owner": "backend/app/middleware/rbac.py",
                             "importers": ["backend/app/dependencies.py"]}
    assert synthetic.facts["symbol_owners"] == {"require_role": "backend/app/middleware/rbac.py"}


def test_error_envelope_follows_openapi(synthetic) -> None:
    env = _by_check(synthetic)["error_envelope"][0]
    assert env.canonical == ["code", "message"]
    assert synthetic.facts["error_envelope"]["schema"] == "Error"


def test_base_path_is_recorded(synthetic) -> None:
    assert synthetic.facts["api_base_path"] == "/v1"
    assert _by_check(synthetic)["api_base_path"][0].severity == INFO


def test_report_and_state_payload_are_serializable(synthetic) -> None:
    payload = synthetic.to_dict()
    json.dumps(payload)
    assert payload["counts"][GAP] == len([f for f in synthetic.findings if f.severity == GAP])
    md = synthetic.to_markdown("proj")
    assert md.startswith("# Pack Preflight Report — proj") and "## Canonical facts" in md


def test_empty_pack_is_quiet() -> None:
    result = run_preflight(PackIndex({}))
    assert result.findings == [] and result.facts == {}


def test_preflight_for_caches_by_content() -> None:
    pkg = _pack()
    assert preflight_for(pkg) is preflight_for(dict(pkg))


@pytest.mark.skipif(not FLEET.is_dir(), reason="Fleet pack not on disk")
def test_fleet_pack_findings_match_its_known_contradictions() -> None:
    from app.services.pack_index import load_pack

    result = preflight_for(load_pack(FLEET))
    checks = _by_check(result)
    assert result.facts["roles"]["values"] == ["administrator", "manager", "technician"]
    assert {"bootstrap_needed", "missing_list_endpoint", "unknown_role"} <= {f.check for f in result.findings if f.severity == GAP}
    vehicle_class = next(f for f in checks["enum_drift"] if "vehicle_class" in f.title)
    assert "`Heavy`" in vehicle_class.detail and "`Heavy Truck`" in vehicle_class.detail
    status = next(f for f in checks["enum_drift"] if "`status`" in f.title)
    assert "`off_road`" in status.detail and "`in_maintenance`" in status.detail
    assert "`closed`" not in status.detail.split("but the pack also uses:")[1]   # valid work-order status
    assert checks["malformed_example"][0].title.startswith("4 ")
    assert result.facts["symbol_owners"]["require_role"].endswith("middleware/rbac.py")
    assert {f.title.split("`")[1] for f in checks["env_name_conflict"]} >= {"SECRET_KEY"}


@pytest.mark.skipif(not AUTH.is_dir(), reason="authentication pack not on disk")
def test_a_coherent_hand_built_pack_has_no_gaps_or_contradictions() -> None:
    from app.services.pack_index import load_pack

    result = preflight_for(load_pack(AUTH))
    assert [f.check for f in result.findings if f.severity in (GAP, NORMALIZED)] == []
