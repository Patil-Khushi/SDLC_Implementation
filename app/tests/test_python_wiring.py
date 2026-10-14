"""Static integration of generated Python/FastAPI code, and the cross-file integration report.

The reconcile pass used to be JavaScript-only. These pin the Python half on the defects the Fleet
product actually shipped with, each reduced to a few lines:

* ``main.py`` imported ``from app.routers import auth`` — the routers are ``app.modules.auth.router``;
* it mounted ``include_router(depots.router, prefix="/depots")`` on a router that already declares
  ``prefix="/depots"`` (every route at ``/depots/depots``);
* 102 imports across 37 files said ``from backend.app.x import y`` — only importable from one level
  ABOVE the project, never in its own Dockerfile;
* ``from app.database import get_db`` where ``get_db`` lives in ``app.dependencies``;
* ``requirements.txt`` lacked ``email-validator`` (EmailStr), ``python-multipart`` (OAuth2 form),
  ``passlib``, ``python-dateutil``.

Fixers act only when exactly one repair exists; everything else is a finding. Idempotence and
"a correct app is left alone" are pinned alongside each fix.
"""

from __future__ import annotations

from app.agents.debugging import _prune_unresolved
from app.graph.nodes import reconcile_node
from app.graph.state import new_state
from app.integrations.executor import FakeExecutor, set_executor
from app.services.python_wiring import (
    PyProject,
    defined_names,
    find_unresolved_python_imports,
    reconcile_fastapi_routers,
    reconcile_python_import_roots,
    reconcile_python_requirements,
)
from app.services.wiring import find_integration_issues, find_unresolved_imports, reconcile_wiring, target_resolves

ROUTER = 'from fastapi import APIRouter\n\nrouter = APIRouter(prefix="/{name}", tags=["{name}"])\n\n\n@router.get("")\ndef list_{name}():\n    return []\n'

MAIN = '''from fastapi import FastAPI


def create_application() -> FastAPI:
    application = FastAPI(title="x")

    from app.routers import auth, depots

    application.include_router(auth.router, prefix="/auth", tags=["auth"])
    application.include_router(
        depots.router, prefix="/depots", tags=["depots"]
    )
    return application


app = create_application()
'''


def _app(**extra: str) -> dict[str, str]:
    files = {
        "backend/app/__init__.py": "",
        "backend/app/main.py": MAIN,
        "backend/app/modules/auth/__init__.py": "",
        "backend/app/modules/auth/router.py": ROUTER.format(name="auth"),
        "backend/app/modules/depots/__init__.py": "",
        "backend/app/modules/depots/router.py": ROUTER.format(name="depots"),
    }
    files.update(extra)
    return files


# --------------------------------------------------------------- the project's modules

def test_import_roots_follow_the_real_packages_not_every_init():
    project = PyProject(_app())
    assert project.dotted_of("backend/app/modules/auth/router.py") == "app.modules.auth.router"
    assert project.lookup("app.modules", "backend/app/main.py") == ("namespace", "")        # no __init__.py
    assert project.lookup("app.modules.auth", "backend/app/main.py") == ("module", "backend/app/modules/auth/__init__.py")


def test_a_src_layout_resolves_from_the_package_root():
    project = PyProject({"backend/src/app/__init__.py": "", "backend/src/app/db.py": "",
                         "backend/tests/__init__.py": "", "backend/tests/test_db.py": "from app.db import x\n"})
    assert project.dotted_of("backend/src/app/db.py") == "app.db"
    assert project.lookup("app.db", "backend/tests/test_db.py") == ("module", "backend/src/app/db.py")


def test_defined_names_are_permissive_and_know_when_they_cannot_know():
    text = "import os\nfrom x import a as b\ntry:\n    def f():\n        pass\nexcept Exception:\n    g = 1\nclass K: ...\nX, Y = 1, 2\n"
    assert {"os", "b", "f", "g", "K", "X", "Y"} <= defined_names(text)
    assert defined_names(text, include_imports=False) >= {"f", "g", "K"} and "b" not in defined_names(text, include_imports=False)
    assert defined_names("from .models import *\n") is None


# --------------------------------------------------------------- unresolved imports

def test_a_missing_module_and_a_missing_name_are_findings_with_their_evidence():
    files = _app(**{
        "backend/app/database.py": "engine = None\n",
        "backend/app/dependencies.py": "def get_db():\n    yield None\n",
        "backend/app/modules/depots/service.py": (
            "from app.database import get_db\nfrom app.modules.depots import router\nfrom app.nowhere import x\n"
            "from fastapi import Depends\nimport os\n"),
    })
    notes = {u.as_note() for u in find_unresolved_python_imports(files)}
    assert ("backend/app/modules/depots/service.py imports 'get_db' from 'app.database' -> backend/app/database.py "
            "does not define it (defined in backend/app/dependencies.py)") in notes
    assert "backend/app/modules/depots/service.py imports 'app.nowhere' -> no such module backend/app/nowhere" in notes
    routers = next(n for n in notes if n.startswith("backend/app/main.py imports 'app.routers' -> no such module backend/app/routers"))
    assert "backend/app/modules/auth/router.py" in routers           # the evidence: where `auth` really is
    assert not any("'router'" in n or "fastapi" in n or "'os'" in n for n in notes)   # submodule, packages


def test_relative_imports_are_judged_too():
    files = {"backend/app/__init__.py": "", "backend/app/a.py": "from .b import thing\nfrom . import c\n",
             "backend/app/b.py": "other = 1\n"}
    notes = [u.as_note() for u in find_unresolved_python_imports(files)]
    assert notes == ["backend/app/a.py imports 'c' from '.' -> backend/app/__init__.py does not define it",
                     "backend/app/a.py imports 'thing' from '.b' -> backend/app/b.py does not define it"]
    files["backend/app/c.py"] = ""                                    # `c` is a submodule once it exists
    assert [u.name for u in find_unresolved_python_imports(files)] == ["thing"]


def test_a_star_import_never_produces_a_missing_name():
    files = {"backend/app/__init__.py": "", "backend/app/models/__init__.py": "from .user import *\n",
             "backend/app/models/user.py": "class User: ...\n", "backend/app/x.py": "from app.models import User, Anything\n"}
    assert find_unresolved_python_imports(files) == []


def test_python_findings_ride_along_with_the_javascript_ones():
    files = {"backend/app/__init__.py": "", "backend/app/a.py": "from app.missing import x\n",
             "frontend/package.json": "{}", "frontend/src/a.ts": "import { b } from './b'\n"}
    importers = [u.importer for u in find_unresolved_imports(files)]
    assert importers == ["frontend/src/a.ts", "backend/app/a.py"]


def test_target_resolves_understands_python_modules_packages_and_namespaces():
    assert target_resolves("backend/app/routers", ["backend/app/routers.py"])
    assert target_resolves("backend/app/routers", ["backend/app/routers/__init__.py"])
    assert target_resolves("backend/app/routers", ["backend/app/routers/auth.py"])
    assert not target_resolves("backend/app/routers", ["backend/app/routers_old.py"])


# --------------------------------------------------------------- @/ alias (frontend)

def test_at_alias_imports_are_resolved_against_the_source_root():
    files = {"frontend/package.json": "{}", "frontend/src/main.tsx": "import x from '@/store/authStore'\nimport y from '@/api/client'\n",
             "frontend/src/api/client.ts": "export default 1\n"}
    assert [u.target for u in find_unresolved_imports(files)] == ["frontend/src/store/authStore"]


def test_at_alias_follows_tsconfig_paths():
    files = {"web/package.json": "{}", "web/tsconfig.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["app/*"]}}}',
             "web/app/x.ts": "import y from '@/y'\n", "web/app/y.ts": "export default 1\n"}
    assert find_unresolved_imports(files) == []


# --------------------------------------------------------------- import roots

def test_imports_prefixed_with_the_project_folder_are_rewritten_to_the_import_root():
    files = _app(**{"backend/app/database.py": "Base = object\n",
                    "backend/app/models/user.py": "from backend.app.database import Base\nimport backend.app.database\n"})
    changed = reconcile_python_import_roots(files)
    assert changed == {"backend/app/models/user.py": "from app.database import Base\nimport backend.app.database\n"}
    assert reconcile_python_import_roots({**files, **changed}) == {}


def test_a_prefixed_import_that_resolves_as_written_is_left_alone():
    files = {"backend/__init__.py": "", "backend/app/__init__.py": "", "backend/app/db.py": "",
             "backend/app/x.py": "from backend.app.db import y\n"}
    assert reconcile_python_import_roots(files) == {}           # backend/ IS a package here


# --------------------------------------------------------------- FastAPI routers

def test_router_imports_from_a_missing_package_are_pointed_at_the_real_routers():
    new = reconcile_fastapi_routers(_app())["backend/app/main.py"]
    assert "from app.routers import" not in new
    assert "    from app.modules.auth import router as auth\n    from app.modules.depots import router as depots\n" in new
    assert "application.include_router(auth.router, tags=[\"auth\"])" in new        # usage unchanged


def test_a_prefix_mounted_twice_is_dropped_from_the_include():
    new = reconcile_fastapi_routers(_app())["backend/app/main.py"]
    assert 'prefix="/auth"' not in new and 'prefix="/depots"' not in new
    assert "depots.router, tags=[\"depots\"]" in new


def test_a_different_include_prefix_is_a_design_choice_and_kept():
    files = _app()
    files["backend/app/main.py"] = MAIN.replace('prefix="/auth"', 'prefix="/api/v1"')
    new = reconcile_fastapi_routers(files)["backend/app/main.py"]
    assert 'prefix="/api/v1"' in new


def test_object_imports_are_fixed_with_the_object():
    files = _app()
    files["backend/app/main.py"] = (
        "from fastapi import FastAPI\nfrom app.api.auth import auth_router\n\napp = FastAPI()\n"
        "app.include_router(auth_router)\n")
    files.pop("backend/app/modules/depots/router.py")
    new = reconcile_fastapi_routers(files)["backend/app/main.py"]
    assert "from app.modules.auth.router import router as auth_router\n" in new


def test_an_ambiguous_router_is_not_guessed():
    files = _app(**{"backend/app/admin/__init__.py": "", "backend/app/admin/auth.py": ROUTER.format(name="auth")})
    new = reconcile_fastapi_routers(files).get("backend/app/main.py", MAIN)
    assert "from app.routers import auth, depots" in new          # two `auth` routers: left for debugging


def test_a_router_nothing_includes_is_mounted_when_it_declares_its_prefix():
    files = _app(**{"backend/app/modules/readings/__init__.py": "",
                    "backend/app/modules/readings/router.py": ROUTER.format(name="readings")})
    new = reconcile_fastapi_routers(files)["backend/app/main.py"]
    assert "from app.modules.readings.router import router as readings_router\n" in new
    assert "    application.include_router(readings_router)\n    return application" in new


def test_a_router_without_its_own_prefix_is_reported_not_mounted():
    files = _app(**{"backend/app/modules/readings/__init__.py": "",
                    "backend/app/modules/readings/router.py": "from fastapi import APIRouter\nrouter = APIRouter()\n"})
    new = reconcile_fastapi_routers(files)["backend/app/main.py"]
    assert "readings" not in new
    kinds = [i["kind"] for i in find_integration_issues({**files, "backend/app/main.py": new})]
    assert "unmounted_router" in kinds


def test_router_reconciliation_is_idempotent_and_leaves_a_correct_app_alone():
    files = _app()
    once = reconcile_fastapi_routers(files)
    assert reconcile_fastapi_routers({**files, **once}) == {}


# --------------------------------------------------------------- requirements

def test_imported_but_undeclared_packages_are_added_to_requirements():
    files = {
        "backend/requirements.txt": "fastapi==0.111.0\nSQLAlchemy==2.0.30\n",
        "backend/app/__init__.py": "",
        "backend/app/schemas.py": "from pydantic import BaseModel, EmailStr\nfrom dateutil import parser\n",
        "backend/app/auth.py": "from fastapi.security import OAuth2PasswordRequestForm\nfrom passlib.context import CryptContext\nimport mystery_lib\n",
        "backend/app/db.py": 'from sqlalchemy import create_engine\nURL = "postgresql://u:p@db/x"\n',
        "backend/tests/test_auth.py": "import pytest\nfrom fastapi.testclient import TestClient\n",
    }
    changed, unknown = reconcile_python_requirements(files)
    assert changed["backend/requirements.txt"].count("# Added by reconciliation") == 1
    added = changed["backend/requirements.txt"].split("not declared\n", 1)[1].split()
    assert added == ["email-validator", "httpx", "passlib[bcrypt]", "psycopg2-binary", "pytest", "python-dateutil",
                     "python-multipart", "uvicorn[standard]"]
    assert "pydantic" not in added                  # provided by fastapi
    assert unknown == ["mystery_lib"]               # never guessed onto PyPI
    assert reconcile_python_requirements({**files, **changed}) == ({}, ["mystery_lib"])


def test_test_only_packages_go_to_a_dev_requirements_file_when_there_is_one():
    files = {"backend/requirements.txt": "fastapi\nuvicorn\n", "backend/requirements-dev.txt": "-r requirements.txt\n",
             "backend/app/__init__.py": "", "backend/app/main.py": "import fastapi\n",
             "backend/tests/test_x.py": "import pytest\n"}
    changed, _ = reconcile_python_requirements(files)
    assert list(changed) == ["backend/requirements-dev.txt"] and "pytest" in changed["backend/requirements-dev.txt"]


def test_no_requirements_file_means_no_change():
    assert reconcile_python_requirements({"backend/app/main.py": "import fastapi\n"}) == ({}, [])


# --------------------------------------------------------------- integration report

def test_routes_outside_the_pack_base_path_are_reported():
    files = {k: v for k, v in reconcile_wiring(_app()).items()}
    issues = find_integration_issues({**_app(), **files}, api_base_path="/v1")
    assert [i["kind"] for i in issues] == ["base_path"] and "/v1" in issues[0]["detail"]
    assert find_integration_issues({**_app(), **files}, api_base_path="") == []


def _mounted_under(prefix: str) -> dict[str, str]:
    files = _app()
    files["backend/app/main.py"] = MAIN.replace('prefix="/auth"', f'prefix="{prefix}"').replace(
        'prefix="/depots"', f'prefix="{prefix}"')
    return {**files, **reconcile_wiring(files)}


def test_routes_under_the_base_path_are_fine():
    assert find_integration_issues(_mounted_under("/v1"), api_base_path="https://api.example.com/v1") == []


def test_a_frontend_calling_a_path_the_backend_does_not_serve_is_reported():
    files = _mounted_under("/v1")
    files["frontend/src/api/client.ts"] = "export const api = axios.create({ baseURL: 'http://localhost:8000/api' })\n"
    issues = find_integration_issues(files)
    assert [(i["kind"], i["files"]) for i in issues] == [("base_path", ["frontend/src/api/client.ts"])]


def test_a_define_once_helper_defined_twice_is_reported():
    files = {"backend/app/middleware/rbac.py": "def require_role(*roles):\n    pass\n",
             "backend/app/dependencies.py": "def require_role(role):\n    pass\n",
             "backend/app/x.py": "from app.middleware.rbac import require_role\n"}
    issues = find_integration_issues(files, symbol_owners={"require_role": "backend/app/middleware/rbac.py"})
    assert [(i["kind"], i["files"]) for i in issues] == [
        ("duplicate_definition", ["backend/app/dependencies.py", "backend/app/middleware/rbac.py"])]


def test_an_async_engine_behind_sync_sessions_is_reported():
    files = {"backend/app/database.py": "from sqlalchemy.ext.asyncio import create_async_engine\n",
             "backend/app/r.py": "from sqlalchemy.orm import Session\ndef f(db: Session = Depends(get_db)):\n    pass\n"}
    assert [i["kind"] for i in find_integration_issues(files)] == ["session_style"]


# --------------------------------------------------------------- the debugging agent's pruning

def test_a_missing_name_is_not_pruned_just_because_the_module_exists():
    finding = {"importer": "backend/app/r.py", "specifier": "app.database", "target": "backend/app/database.py",
               "candidates": [], "name": "get_db"}
    kept = _prune_unresolved([finding], project_dir="p", generated_code=["p/backend/app/database.py"], fixes=[])
    assert kept == [finding]
    fixed_target = [{"path": "backend/app/database.py", "content": "def get_db():\n    pass\n"}]
    assert _prune_unresolved([finding], project_dir="p", generated_code=[], fixes=fixed_target) == []
    fixed_importer = [{"path": "backend/app/r.py", "content": "from app.dependencies import get_session\n"}]
    assert _prune_unresolved([finding], project_dir="p", generated_code=[], fixes=fixed_importer) == []


# --------------------------------------------------------------- the reconcile node

def test_the_reconcile_node_fixes_python_wiring_and_records_the_rest():
    project = "proj"
    files = _app(**{"backend/requirements.txt": "fastapi\n",
                    "backend/app/models/user.py": "from backend.app.database import Base\nfrom app.database import get_db\n",
                    "backend/app/database.py": "Base = object\n"})
    executor = FakeExecutor()
    for rel, content in files.items():
        executor.write_file(f"{project}/{rel}", content)
    state = new_state(run_id="r", attempt=1, project_id=project, design_package={})
    state["generated_code"] = [f"{project}/{rel}" for rel in files]
    set_executor(executor)
    try:
        out = reconcile_node(state)
    finally:
        set_executor(None)
    assert "from app.modules.auth import router as auth" in executor.files[f"{project}/backend/app/main.py"]
    assert executor.files[f"{project}/backend/app/models/user.py"].startswith("from app.database import Base")
    assert "uvicorn[standard]" in executor.files[f"{project}/backend/requirements.txt"]
    notes = [f"{u['specifier']}:{u.get('name', '')}" for u in out["unresolved_imports"]]
    assert notes == ["app.database:get_db"]
    assert out["integration_findings"] == []
