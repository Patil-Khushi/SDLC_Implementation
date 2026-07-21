"""Naming contract (Track B, B1/B2) in ``scripts/feature_commit.py``.

Root cause of "the generated pieces don't fit together": each file is generated in its OWN isolated
call and re-derives identifiers, so routers/schemas/services/models/frontend drift apart. The fix
distills the pack's authoritative names (DB schema + API mapping) into ONE contract block that is
prepended to EVERY generation call, and reorders the layers so models precede the services/routers
that use them. These tests pin the builder (Mongo + SQL + CSV), graceful degradation, the actual
injection into the chunked-generation prompts, and the layer order — all offline (no LLM/network).
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import scripts.feature_commit as fc


class _ScriptedGateway:
    """Serves canned ``.complete()`` replies in order and records every prompt for inspection."""

    def __init__(self, responses: list[str]) -> None:
        self._queue: deque[str] = deque(responses)
        self.calls: list[str] = []

    def complete(self, prompt: str, *, system: str | None = None, max_tokens: int | None = None) -> str:
        self.calls.append(prompt)
        return self._queue.popleft() if self._queue else '{"files":[]}'


# ------------------------------------------------------------------- Mongo (db_schema.json)

def test_contract_from_mongo_schema(tmp_path: Path) -> None:
    (tmp_path / "db_schema.json").write_text(json.dumps({
        "datastore": "document", "odm": "mongoose",
        "collections": [
            {"name": "users", "model": "User", "fields": [
                {"name": "email"}, {"name": "passwordHash"}, {"name": "role"}]},
            {"name": "orders", "model": "Order", "fields": [{"name": "total"}]},
        ],
    }), encoding="utf-8")

    contract = fc.build_naming_contract(tmp_path)

    assert "`User`" in contract and "`users`" in contract
    # Exact field names carried verbatim (the drift the contract prevents: passwordHash, not password_hash).
    assert "passwordHash" in contract
    assert "`Order`" in contract
    assert contract.isascii()  # no stray unicode that a cp1252 console/log could choke on


# ------------------------------------------------------------------- SQL (schema.sql)

def test_contract_from_sql_schema(tmp_path: Path) -> None:
    (tmp_path / "schema.sql").write_text(
        """
        CREATE TABLE users (
            id             UUID         NOT NULL DEFAULT gen_random_uuid(),
            full_name      VARCHAR(120) NOT NULL,
            email          CITEXT       NOT NULL,
            password_hash  VARCHAR(255) NOT NULL,
            CONSTRAINT users_pkey PRIMARY KEY (id),
            CONSTRAINT users_email_unique UNIQUE (email)
        );
        """,
        encoding="utf-8",
    )

    contract = fc.build_naming_contract(tmp_path)

    # Table -> singular PascalCase model class; exact snake_case columns (full_name, not fullName).
    assert "`User`" in contract and "`users`" in contract
    assert "full_name" in contract and "password_hash" in contract
    # Constraint lines are NOT mistaken for columns.
    assert "CONSTRAINT" not in contract and "users_pkey" not in contract


def test_sql_parser_extracts_only_column_names(tmp_path: Path) -> None:
    sql = "CREATE TABLE t (\n  a INT,\n  b TEXT,\n  PRIMARY KEY (a),\n  CHECK (b <> '')\n);"
    (tmp_path / "schema.sql").write_text(sql, encoding="utf-8")
    entities = fc._entities_from_sql(sql)
    assert entities == [{"model": "T", "store": "t", "fields": ["a", "b"]}]


# ------------------------------------------------------------------- endpoints (CSV, both shapes)

def test_endpoints_from_legacy_api_mapping(tmp_path: Path) -> None:
    (tmp_path / "api-mapping.csv").write_text(
        "http_method,endpoint_path,operation_id\n"
        "POST,/auth/login,login\n"
        "POST,/auth/login,login\n"          # duplicate row -> deduped
        "GET,/auth/me,me\n",
        encoding="utf-8",
    )
    contract = fc.build_naming_contract(tmp_path)
    assert "POST /auth/login -> login" in contract
    assert contract.count("POST /auth/login -> login") == 1  # deduped
    assert "GET /auth/me -> me" in contract


def test_endpoints_from_api_to_ui_mapping_and_ragged_rows(tmp_path: Path) -> None:
    # api-to-ui-mapping.csv uses a combined api_endpoint column; include a ragged row (extra commas)
    # which csv.DictReader parks under a None restkey — must not crash (regression guard).
    (tmp_path / "api-to-ui-mapping.csv").write_text(
        "screen,ui_field,direction,api_endpoint,api_field\n"
        "Login,Email,send,POST /auth/login,email\n"
        "Login,Note,send,POST /auth/login,note,extra1,extra2\n",
        encoding="utf-8",
    )
    contract = fc.build_naming_contract(tmp_path)
    assert "POST /auth/login" in contract


# ------------------------------------------------------------------- graceful degradation

def test_empty_pack_returns_blank(tmp_path: Path) -> None:
    assert fc.build_naming_contract(tmp_path) == ""


def test_garbage_schema_returns_blank(tmp_path: Path) -> None:
    (tmp_path / "db_schema.json").write_text("{ not valid json", encoding="utf-8")
    assert fc.build_naming_contract(tmp_path) == ""


# ------------------------------------------------------------------- injection into prompts

def test_prepend_contract_puts_contract_first() -> None:
    assert fc._prepend_contract("CONTRACT", "CTX") == "CONTRACT\n\nCTX"
    assert fc._prepend_contract("", "CTX") == "CTX"            # no contract -> unchanged (no-op)
    assert fc._prepend_contract("CONTRACT", "") == "CONTRACT"  # no other context


def test_contract_reaches_layer_prompt() -> None:
    ctx = fc._prepend_contract("NAMING CONTRACT -- use exact names", "design ctx")
    prompt = fc._layer_prompt(ctx, {}, "US-01", "Login", "body", "BACKEND", "do it")
    assert "NAMING CONTRACT -- use exact names" in prompt


def test_contract_reaches_chunked_manifest_and_file_prompts() -> None:
    ctx = fc._prepend_contract("NAMING CONTRACT -- token XYZ", "design ctx")

    gw_m = _ScriptedGateway(['{"files":[{"path":"backend/app/models/user.py","purpose":"x"}]}'])
    fc._layer_manifest(gw_m, ctx, [], "US-01", "Login", "body", "DATABASE", "do it")
    assert any("token XYZ" in p for p in gw_m.calls)

    gw_f = _ScriptedGateway(['{"files":[{"path":"backend/app/models/user.py","content":"x=1"}]}'])
    fc._generate_file(gw_f, ctx, "US-01", "Login", "body", "DATABASE",
                      "backend/app/models/user.py", "the model", ["backend/app/models/user.py"], {})
    assert any("token XYZ" in p for p in gw_f.calls)


# ------------------------------------------------------------------- layer order (B2)

def test_layers_generate_database_before_backend() -> None:
    keys = [key for key, _label, _instr in fc._LAYERS]
    assert keys[0] == "frontend"
    assert keys.index("database") < keys.index("backend")  # models exist before services/routers use them
    # The full set is unchanged — only the order moved.
    assert set(keys) == {"frontend", "database", "backend", "integration", "testing"}
