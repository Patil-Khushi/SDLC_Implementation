"""The scaffold describes a runtime the generated app can actually start in.

``docker compose up`` used to start a Postgres with no credentials (its image refuses to start
without ``POSTGRES_PASSWORD``), a backend that raced it with a bare ``depends_on``, and a frontend
container with no source mounted. ``.env.example`` listed two bare keys while the pack's own env
file declared thirty, and the database driver was always ``psycopg`` even when the pack's DSN said
``postgresql+asyncpg://``. And a work item that rewrote ``package.json`` silently dropped the
scaffold's test harness.
"""

from __future__ import annotations

import json

import yaml

from app.services.boilerplate import render_scaffold
from app.services.wiring import reconcile_scaffold_manifests

PACK = {
    "env-vars.json": {"groups": [{"name": "db", "vars": [
        {"key": "DATABASE_URL", "example": "postgresql+asyncpg://u:p@localhost:5432/fleet", "secret": True,
         "comment": "async DSN"},
        {"key": "JWT_SECRET_KEY", "example": "abc", "secret": True, "comment": "signing key"},
        {"key": "TELEMATICS_MODE", "example": "mock", "comment": "mock | live"},
    ]}]},
    "skills.md": "\n".join(["# Conventions"] + [f"- Always hash passwords with bcrypt, rule {n}; never store plaintext."
                                                for n in range(9)]),
}


def _files(pack=None) -> dict[str, str]:
    return {f["path"]: f["content"] for f in render_scaffold("fleet", pack)}


def test_compose_gives_the_database_credentials_a_healthcheck_and_waits_for_it():
    compose = yaml.safe_load(_files(PACK)["docker-compose.yml"])
    db = compose["services"]["db"]
    assert db["environment"]["POSTGRES_PASSWORD"] and "pg_isready" in " ".join(db["healthcheck"]["test"])
    backend = compose["services"]["backend"]
    assert backend["depends_on"] == {"db": {"condition": "service_healthy"}}
    assert backend["environment"]["DATABASE_URL"] == "postgresql+asyncpg://app:app@db:5432/app"   # the pack's scheme
    assert backend["env_file"] == [{"path": ".env", "required": False}]


def test_compose_mounts_and_installs_the_frontend():
    frontend = yaml.safe_load(_files(PACK)["docker-compose.yml"])["services"]["frontend"]
    assert "./frontend:/app" in frontend["volumes"] or "./.:/app" in frontend["volumes"]
    assert "npm install" in " ".join(frontend["command"])


def test_the_default_scaffold_compose_is_valid_and_uses_the_default_driver():
    compose = yaml.safe_load(_files()["docker-compose.yml"])
    assert compose["services"]["backend"]["environment"]["DATABASE_URL"].startswith("postgresql+psycopg://")


def test_env_example_lists_what_the_pack_declares_with_secrets_blank():
    env = _files(PACK)[".env.example"]
    assert "# mock | live\nTELEMATICS_MODE=mock" in env
    assert "# signing key\nJWT_SECRET_KEY=\n" in env                  # secret: never an example value
    assert "\nDATABASE_URL=\n" in env and "SECRET_KEY=" in env


def test_the_driver_and_hashing_follow_the_pack():
    reqs = next(v for k, v in _files(PACK).items() if k.endswith("requirements.txt"))
    assert "asyncpg>=" in reqs and "bcrypt>=" in reqs
    default = next(v for k, v in _files().items() if k.endswith("requirements.txt"))
    assert "asyncpg" not in default and "\nbcrypt" not in default


# --------------------------------------------------------------- the scaffold's harness survives

SCAFFOLD_PKG = json.dumps({"name": "fe", "scripts": {"dev": "vite", "test": "jest"},
                           "devDependencies": {"jest": "^29.7.0", "babel-jest": "^29.7.0"}}, indent=2) + "\n"


def test_a_rewritten_manifest_gets_back_the_harness_it_dropped():
    generated = json.dumps({"name": "fe", "scripts": {"dev": "vite --port 5173"},
                            "dependencies": {"react": "^18"}, "devDependencies": {"jest": "^30.0.0"}}, indent=2) + "\n"
    changed = reconcile_scaffold_manifests({"frontend/package.json": generated}, {"frontend/package.json": SCAFFOLD_PKG})
    pkg = json.loads(changed["frontend/package.json"])
    assert pkg["scripts"] == {"dev": "vite --port 5173", "test": "jest"}          # generated values win
    assert pkg["devDependencies"] == {"babel-jest": "^29.7.0", "jest": "^30.0.0"}
    again = reconcile_scaffold_manifests({"frontend/package.json": changed["frontend/package.json"]},
                                         {"frontend/package.json": SCAFFOLD_PKG})
    assert again == {}


def test_an_own_test_script_and_an_unparseable_manifest_are_left_alone():
    own = json.dumps({"scripts": {"test": "vitest"}, "devDependencies": {"jest": "1", "babel-jest": "1"}})
    assert reconcile_scaffold_manifests({"frontend/package.json": own}, {"frontend/package.json": SCAFFOLD_PKG}) == {}
    assert reconcile_scaffold_manifests({"frontend/package.json": "{ nope"}, {"frontend/package.json": SCAFFOLD_PKG}) == {}
