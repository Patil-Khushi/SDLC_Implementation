"""One loader + one shape-based index over a design package.

The failure this guards against: lookups by FILENAME, where the names known were one pack format's
(``SKILL.md``, ``schema.sql``, Mongo ``db_schema.json``). The current Design-team format ships
``skills.md``, ``db_schema.sql`` and a RELATIONAL ``db_schema.json`` — so its conventions and schema
never reached code generation. Every artifact here is found by content, whatever it is called.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import design_pack
from app.services.pack_index import PackIndex, index_for, load_pack

_REPO = Path(__file__).resolve().parents[2]
ON_DISK_PACKS = [p for p in (_REPO / "uploads/authentication", _REPO / "fixtures/resources", _REPO / "uploads/v1") if p.is_dir()]

OPENAPI_YAML = """openapi: 3.1.0
info: {title: T, version: '1'}
paths:
  /auth/login:
    post:
      operationId: login
      tags: [auth]
      requestBody:
        content:
          application/json:
            schema: {$ref: '#/components/schemas/LoginRequest'}
            example: {email: a@b.c, since: 2024-08-15}
      responses: {'200': {description: ok}}
components:
  schemas:
    LoginRequest: {type: object, required: [email], properties: {email: {type: string}}}
"""
SQL = """CREATE TABLE users (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    role varchar(30) NOT NULL,
    depot_id uuid,
    PRIMARY KEY (id),
    CHECK (role IN ('administrator', 'manager', 'technician'))
);
CREATE TABLE depots (id uuid NOT NULL, PRIMARY KEY (id));
ALTER TABLE users ADD CONSTRAINT fk_users_depot FOREIGN KEY (depot_id) REFERENCES depots (id);
"""
RELATIONAL_JSON = {
    "datastore": "relational",
    "tables": [{"name": "users", "columns": [{"name": "id", "type": "uuid", "nullable": False},
                                            {"name": "role", "type": "varchar(30)", "nullable": False}],
                "primary_key": ["id"], "checks": [], "foreign_keys": []}],
}
CONVENTIONS_MD = "## Coding Guidelines\n" + "".join(f"- Rule {i}: always do thing {i}.\n" for i in range(10))
TOKENS = {"color": {f"c{i}": f"#{i:06x}" for i in range(10)}, "spacing": {"s1": "4px", "s2": "8px"}}
VALIDATION = {"login": {"email": {"required": True, "message": "Enter a valid email."},
                        "password": {"required": True, "minLength": 8, "message": "Too short."}}}
MOCKUP = (
    "<!DOCTYPE html><html><body>"
    '<section class="screen" id="screen-login"><h1>Login</h1><section id="inner">x</section></section>'
    '<section class="screen" id="screen-forgot"><h1>Forgot</h1></section>'
    "</body></html>"
)
STATES = (
    "Login:\n- Loading → spinner\n- Empty → N/A\n- Error → banner\n- Success → redirect\n\n"
    "Vehicle List:\n- Loading → shimmer\n- Empty → message\n- Error → retry\n"
)
SAMPLES = {"login": {"method": "POST", "path": "/auth/login", "request": {}, "responses": {}},
           "me": {"method": "GET", "path": "/auth/me", "responses": {}}}
ENV_JSON = {"groups": [{"name": "Core", "vars": [{"key": "DATABASE_URL", "example": "", "secret": True, "required": True}]}]}
ENV_TEXT = "# Core\n# Database DSN  [required]\nDATABASE_URL=\n# signing key\nJWT_SECRET_KEY=\nPORT=8000\n"
REQUIREMENTS = {"functional": [{"id": "REQ-1", "text": "Login works.", "feature": "Auth"}],
                "non_functional": [{"id": "NFR-1", "text": "Hash passwords."}],
                "performance": [{"id": "NFR-9", "text": "Fast."}],
                "business_rules": [{"id": "BR-1", "text": "12,000 km or 6 months."}],
                "constraints": ["Core loop only."]}


def _write_pack(root: Path, files: dict[str, object]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        elif isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_text(json.dumps(content), encoding="utf-8")
    return root


# --------------------------------------------------------------------------- load_pack

def test_load_pack_keeps_top_level_names_adds_subfolders_and_skips_noise(tmp_path: Path) -> None:
    pack = _write_pack(tmp_path, {
        "openapi.yaml": OPENAPI_YAML,
        "tokens.json": TOKENS,
        "er_diagrams/manager.md": "erDiagram\n  users ||--o{ depots : x\n",
        ".api_contract_state.json": {"stale": True},
        "api_contract.state.json": {"stale": True},
        ".signature.json": {"signature": "x"},
        "_handoff/SRS.docx": b"PK\x03\x04\x00\xff\xfe binary",
        ".git/config": "[core]",
    })
    pkg = load_pack(pack)
    assert set(pkg) == {"openapi.yaml", "tokens.json", "er_diagrams/manager.md"}
    assert isinstance(pkg["tokens.json"], dict)          # JSON parsed
    assert isinstance(pkg["openapi.yaml"], str)          # YAML stays text (JSON-compatible package)


def test_pipeline_state_files_are_recognised() -> None:
    for name in (".api_contract_state.json", ".orchestrator_state.json", "api_contract.state.json", ".signature.json"):
        assert design_pack.is_pipeline_state_file(name), name
    for name in ("backend-structure.json", "state_transitions.md", "db_schema.json"):
        assert not design_pack.is_pipeline_state_file(name), name


def test_detect_roles_no_longer_picks_a_stale_state_file_as_the_backend_tree(tmp_path: Path) -> None:
    tree = {"root/": {"backend/": {"app/": {"main.py": "FastAPI app factory", "router.py": "routes"}}}}
    pack = _write_pack(tmp_path, {".backend_structure_state.json": {"structure": tree}, "backend-structure.json": tree})
    roles = design_pack.detect_roles(pack)
    assert [d.path.name for d in roles["backend_structure"]] == ["backend-structure.json"]


# --------------------------------------------------------------------------- classification

@pytest.mark.parametrize(("name", "value", "role"), [
    ("openapi.yaml", OPENAPI_YAML, "openapi"),
    ("whatever-name.txt", "CREATE TABLE x (id int);", "db_schema"),
    ("db_schema.sql", SQL, "db_schema"),
    ("db_schema.json", RELATIONAL_JSON, "db_schema"),
    ("skills.md", CONVENTIONS_MD, "conventions"),
    ("my-team-rules.md", CONVENTIONS_MD, "conventions"),
    ("tokens.json", TOKENS, "design_tokens"),
    ("validation-rules.json", VALIDATION, "validation_rules"),
    ("mockup.html", MOCKUP, "mockup"),
    ("state_transitions.md", STATES, "state_transitions"),
    ("sample-payloads.json", SAMPLES, "sample_payloads"),
    ("env-vars.json", ENV_JSON, "env_vars"),
    (".env.example", ENV_TEXT, "env_vars"),
    ("er_diagrams/admin.md", "erDiagram\n  a ||--o{ b : c\n", "diagrams"),
    ("api-to-ui-mapping.csv", "screen,ui_field,direction,api_endpoint,api_field\nLogin,Email,send,POST /auth/login,email\n", "api_ui_mapping"),
    ("extracted_requirements.json", REQUIREMENTS, "requirements"),
    (".db_schema_state.json", RELATIONAL_JSON, "pipeline_state"),
])
def test_classify_value_finds_roles_by_content_not_name(name: str, value: object, role: str) -> None:
    assert design_pack.classify_value(name, value)[0] == role


def test_in_memory_classification_agrees_with_detect_roles_on_planning_roles() -> None:
    planning = {"openapi", "api_ui_mapping", "rich_api_mapping", "db_schema", "backend_structure",
                "frontend_structure", "requirements", "user_features", "routes"}
    for pack in ON_DISK_PACKS:
        on_disk = {r: sorted(d.path.name for d in v) for r, v in design_pack.detect_roles(pack).items() if r in planning}
        in_memory = {r: sorted(n for n in names if "/" not in n)
                     for r, names in PackIndex.from_dir(pack).roles().items() if r in planning}
        assert in_memory == on_disk, pack.name


# --------------------------------------------------------------------------- accessors

def _index(**files: object) -> PackIndex:
    return PackIndex({name: value for name, value in files.items()})


def test_openapi_from_yaml_text_keeps_dates_as_strings() -> None:
    spec = _index(**{"api.yaml": OPENAPI_YAML}).openapi()
    assert spec is not None and "/auth/login" in spec["paths"]
    example = spec["paths"]["/auth/login"]["post"]["requestBody"]["content"]["application/json"]["example"]
    assert example["since"] == "2024-08-15"               # not a datetime.date — stays JSON-compatible
    json.dumps(spec)


def test_tables_prefer_structured_columns_and_merge_sql_enums_and_fks() -> None:
    tables = {t["name"]: t for t in _index(**{"db_schema.json": RELATIONAL_JSON, "db_schema.sql": SQL}).tables()}
    assert list(tables) == ["users"]                       # the structured source with columns wins
    assert tables["users"]["enums"]["role"] == ["administrator", "manager", "technician"]   # merged from SQL


def test_tables_from_sql_only() -> None:
    tables = {t["name"]: t for t in _index(**{"schema-any-name.sql": SQL}).tables()}
    users = tables["users"]
    assert [c["name"] for c in users["columns"]] == ["id", "role", "depot_id"]
    assert users["primary_key"] == ["id"]
    assert users["columns"][2]["nullable"] is True and users["columns"][1]["nullable"] is False
    assert users["enums"]["role"] == ["administrator", "manager", "technician"]
    assert users["foreign_keys"] == [{"columns": ["depot_id"], "ref_table": "depots", "ref_columns": ["id"]}]


def test_tables_from_mongo_collections() -> None:
    mongo = {"collections": [{"name": "orders", "fields": [{"name": "status", "type": "string", "enum": ["new", "paid"]}]}]}
    table = _index(**{"db_schema.json": mongo}).tables()[0]
    assert table["name"] == "orders" and table["enums"] == {"status": ["new", "paid"]}


def test_conventions_prefer_the_richest_markdown_then_json() -> None:
    short = "## Guidelines\n- always a\n"
    assert _index(**{"SKILL.md": CONVENTIONS_MD, "skills.md": short}).conventions_text() == CONVENTIONS_MD.strip()
    rendered = _index(**{"coding_guidelines.json": {"categories": [{"name": "API", "rules": ["Use async."]}]}}).conventions_text()
    assert rendered == "### API\n- Use async."


def test_screen_section_matches_exact_and_abbreviated_ids_whole_element() -> None:
    idx = _index(**{"mockup.html": MOCKUP})
    login = idx.screen_section("Login")
    assert login.startswith('<section class="screen" id="screen-login">') and login.endswith("</section></section>")
    assert "Forgot" in idx.screen_section("Forgot Password")         # id is just "screen-forgot"
    assert idx.screen_section("Vehicle List") == ""


def test_state_transitions_block_for_a_screen() -> None:
    idx = _index(**{"state_transitions.md": STATES})
    block = idx.state_transitions("Vehicle List")
    assert block.startswith("Vehicle List:") and "shimmer" in block and "spinner" not in block


def test_requirements_carry_text_and_fold_performance_into_nfr() -> None:
    idx = _index(**{"extracted_requirements.json": REQUIREMENTS})
    req = idx.requirements()
    assert [r["id"] for r in req["non_functional"]] == ["NFR-1", "NFR-9"]
    assert idx.requirement_text(["BR-1", "REQ-1"]) == [REQUIREMENTS["business_rules"][0], REQUIREMENTS["functional"][0]]


def test_env_vars_merge_structured_metadata_with_text_only_keys() -> None:
    keys = {v["key"]: v for v in _index(**{"env-vars.json": ENV_JSON, ".env.example": ENV_TEXT}).env_vars()}
    assert set(keys) == {"DATABASE_URL", "JWT_SECRET_KEY", "PORT"}
    assert keys["DATABASE_URL"]["secret"] is True and keys["PORT"]["example"] == "8000"


def test_index_for_caches_by_content_not_identity() -> None:
    a = {"tokens.json": TOKENS}
    assert index_for(a) is index_for(dict(a))                 # same content -> same index
    assert index_for(a) is not index_for({"tokens.json": {**TOKENS, "extra": {"x": "1px"}}})


def test_missing_artifacts_degrade_to_empty_values() -> None:
    idx = PackIndex({})
    assert idx.openapi() is None and idx.tables() == [] and idx.conventions_text() == ""
    assert idx.tokens() is None and idx.screen_section("Login") == "" and idx.env_vars() == []


@pytest.mark.parametrize("pack", ON_DISK_PACKS, ids=lambda p: p.name)
def test_every_on_disk_pack_yields_spec_schema_conventions_and_rules(pack: Path) -> None:
    idx = PackIndex.from_dir(pack)
    assert idx.openapi() and idx.openapi().get("paths")
    assert idx.tables() and all(t["columns"] for t in idx.tables())
    assert idx.conventions_text()
    assert idx.tokens() and idx.validation_rules()
    assert idx.requirements()["functional"]
