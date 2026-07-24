"""The naming contract (app/services/naming_contract.py) and its injection into the LangGraph
code_generator agent.

The unit tests for the extractors themselves live in ``test_naming_contract.py`` (exercised via the
``scripts/feature_commit`` re-exports). These pin the NEW behaviour: (1) a contract can be built
from an in-memory ``design_package`` dict (the shape the graph agent holds, not a pack directory),
and (2) the code_generator prepends it to EVERY work item's context so isolated per-file generation
still agrees on entity/field/endpoint names.
"""

from __future__ import annotations

import json

from app.agents.code_generator import CodeGeneratorAgent
from app.models import WorkItem
from app.services.naming_contract import (
    build_naming_contract,
    build_naming_contract_from_package,
)

_MONGO_PACK = {
    "db_schema.json": {
        "datastore": "document",
        "collections": [
            {"name": "users", "model": "User", "fields": [{"name": "email"}, {"name": "passwordHash"}]},
        ],
    },
    "api-to-ui-mapping.csv": "screen,ui_field,direction,api_endpoint,api_field\nLogin,Email,send,POST /auth/login,email\n",
}


def test_from_package_builds_contract_from_parsed_dict() -> None:
    # The graph holds already-parsed JSON (a dict), not raw text — the builder must coerce it.
    contract = build_naming_contract_from_package(_MONGO_PACK)
    assert "`User`" in contract and "`users`" in contract
    assert "passwordHash" in contract          # exact field name carried verbatim
    assert "POST /auth/login" in contract       # endpoint from the api-to-ui mapping


def test_from_package_matches_disk_builder(tmp_path) -> None:
    # Same inputs on disk vs in-memory must render an identical block (single renderer).
    (tmp_path / "db_schema.json").write_text(json.dumps(_MONGO_PACK["db_schema.json"]), encoding="utf-8")
    (tmp_path / "api-to-ui-mapping.csv").write_text(_MONGO_PACK["api-to-ui-mapping.csv"], encoding="utf-8")
    assert build_naming_contract_from_package(_MONGO_PACK) == build_naming_contract(tmp_path)


def test_empty_package_yields_no_contract() -> None:
    assert build_naming_contract_from_package({}) == ""
    assert build_naming_contract_from_package(None) == ""


def test_contract_is_injected_first_into_code_generator_context() -> None:
    agent = CodeGeneratorAgent()
    item = WorkItem(id="WI-1", endpoints=["POST /auth/login"], tables=["users"],
                    target_files=["src/modules/auth/auth.service.js"])
    context, sections = agent._assemble_context(item, _MONGO_PACK)

    assert sections and sections[0] == "Naming contract"     # leads the context
    assert "NAMING CONTRACT" in context
    assert context.index("NAMING CONTRACT") == 0             # literally first bytes


def test_no_contract_section_when_package_has_no_schema_or_api() -> None:
    agent = CodeGeneratorAgent()
    item = WorkItem(id="WI-2", screens=["Login"], target_files=["src/pages/Login.jsx"])
    _context, sections = agent._assemble_context(item, {"SKILL.md": "Use camelCase."})
    assert "Naming contract" not in sections


def test_produced_files_manifest_is_injected() -> None:
    agent = CodeGeneratorAgent()
    item = WorkItem(id="WI-3", screens=["Home"], target_files=["src/App.jsx"])
    produced = ["package.json", "src/routes/index.jsx", "src/pages/Home.jsx"]
    context, sections = agent._assemble_context(item, {"SKILL.md": "x"}, produced)

    assert "Existing files" in sections
    assert "src/routes/index.jsx" in context and "src/pages/Home.jsx" in context


def test_already_generated_strips_project_prefix() -> None:
    from app.graph.state import new_state

    agent = CodeGeneratorAgent()
    state = new_state(run_id="r", attempt=1, project_id="p1", design_package={})
    state["generated_code"] = ["p1/package.json", "p1/src/App.jsx", "p1/src/App.jsx"]  # dupe ignored
    assert agent._already_generated(state) == ["package.json", "src/App.jsx"]


def test_no_existing_files_section_on_first_item() -> None:
    agent = CodeGeneratorAgent()
    item = WorkItem(id="WI-4", screens=["Home"], target_files=["src/App.jsx"])
    _context, sections = agent._assemble_context(item, {"SKILL.md": "x"}, [])
    assert "Existing files" not in sections
