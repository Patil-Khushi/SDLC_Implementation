"""Fewer debugging rounds and ~80% fewer tests.

A real Fleet run showed two things. The debugging agent converged (225 -> 1 error) but needed eight
rounds, because generation left drift behind: files saw earlier files only as PATHS, so pages
imported ``ApiError``/``User``/``AsyncSessionLocal`` names that did not exist and 17 CSS modules
nobody generated. And unit-test generation wrote 1,325 test functions over 39 files (a third of
them testing declarative models/schemas/settings) for a stage that precedes a dedicated Testing phase.
"""

from __future__ import annotations

import ast
import json

from app.agents.code_generator import CodeGeneratorAgent
from app.agents.unit_test import UnitTestAgent, _testable, unit_test_cap, trim_python_tests
from app.graph.state import new_state
from app.integrations.executor import FakeExecutor
from app.models import WorkItem
from app.services.export_map import extract_exports, js_exported_names, js_imports, render_exports
from app.services.llm_gateway import FakeLLMGateway
from app.services.wiring import find_unresolved_imports, reconcile_missing_styles, reconcile_wiring

# --------------------------------------------------------------- exports map

DB = "from sqlalchemy import x\n\nengine = create_async_engine(URL)\nSessionLocal = async_sessionmaker(engine)\n\n\nclass Base:\n    pass\n\n\nasync def get_db() -> AsyncIterator[Session]:\n    yield 1\n\n\ndef _private():\n    pass\n"
COMMON = "export interface Error { code: string }\nexport type UserRole = 'admin'\nexport const API = 1\nexport default function Page() {}\n"


def test_python_exports_list_public_names_with_signatures():
    out = extract_exports("backend/app/database.py", DB)
    assert "class Base" in out and "async def get_db() -> AsyncIterator[Session]:" in out[1] + out[2] or any("get_db" in o for o in out)
    assert any(o.startswith("SessionLocal =") for o in out)
    assert not any("_private" in o for o in out)


def test_ts_exports_say_named_or_default():
    out = extract_exports("frontend/src/types/common.ts", COMMON)
    assert "named interface Error { code: string }" in out
    assert any(o.startswith("default function Page") for o in out) and any(o.startswith("named const API") for o in out)


def test_the_block_names_files_it_cannot_fit_and_filters_by_side():
    files = {"backend/a.py": ["def a()"], "frontend/b.ts": ["named const b"] * 3, "frontend/c.ts": ["named const c"]}
    text = render_exports(files, side="frontend", cap=70)
    assert "### frontend/b.ts" in text and "backend/a.py" not in text and "symbols not shown: frontend/c.ts" in text
    assert render_exports({}, side="frontend") == ""


def _state(files: dict[str, str]):
    state = new_state(run_id="r", attempt=1, project_id="p", design_package={})
    state["generated_code"] = [f"p/{rel}" for rel in files]
    return state


def test_generation_sees_what_earlier_files_export():
    files = {"frontend/src/types/common.ts": COMMON, "backend/app/database.py": DB}
    executor = FakeExecutor(files={f"p/{rel}": text for rel, text in files.items()})
    item = WorkItem(id="frontend-pages", target_files=["frontend/src/pages/A.tsx", "frontend/src/pages/B.tsx"])
    llm = FakeLLMGateway(lambda prompt: json.dumps({"files": [{"path": prompt.rsplit("now: ", 1)[-1].split("\n")[0].strip(),
                                                              "content": "x"}]}))
    state = _state(files)
    state["current_work_item"] = item
    CodeGeneratorAgent(executor=executor, llm=llm).execute(state)
    prompt = llm.calls[0]["prompt"]
    assert "What the files already generated EXPORT" in prompt
    assert "named interface Error { code: string }" in prompt          # the real name ...
    assert "SessionLocal" not in prompt                                # ... and only this side's files


# --------------------------------------------------------------- import checking

def test_a_named_import_the_module_does_not_export_is_reported_with_where_it_lives():
    files = {"frontend/package.json": "{}", "frontend/src/types/common.ts": COMMON,
             "frontend/src/types/other.ts": "export type ApiError = string\n",
             "frontend/src/pages/P.tsx": "import { ApiError, UserRole } from '@/types/common'\n"}
    notes = [u.as_note() for u in find_unresolved_imports(files)]
    assert notes == ["frontend/src/pages/P.tsx imports 'ApiError' from '@/types/common' -> "
                     "frontend/src/types/common.ts does not define it (defined in frontend/src/types/other.ts)"]


def test_a_default_import_of_a_module_with_no_default_is_reported():
    files = {"frontend/package.json": "{}", "frontend/src/Button.tsx": "export function Button() {}\n",
             "frontend/src/P.tsx": "import Button from './Button'\nimport { Button as B } from './Button'\n"}
    assert [u.name for u in find_unresolved_imports(files)] == ["default"]


def test_unknowable_namespaces_are_never_judged():
    files = {"frontend/package.json": "{}", "frontend/src/index.ts": "export * from './a'\n", "frontend/src/a.ts": "export const a = 1\n",
             "frontend/src/cjs.js": "module.exports = { a: 1 }\n",
             "frontend/src/P.tsx": "import { a, nope } from './index'\nimport { z } from './cjs'\n"}
    assert find_unresolved_imports(files) == []


def test_import_clause_parsing():
    parsed = js_imports("import React, { useState, type FC as F } from 'react'\nimport * as ns from './x'\nimport './side'\n"
                        "import type { T } from './t'\n")
    assert parsed == [("react", ["useState", "FC"], "React"), ("./t", ["T"], "")]
    assert js_exported_names("export { a as b, type C }\nexport default 1\n") == ({"b", "C"}, True, False)


def test_a_missing_stylesheet_is_created_as_a_visible_placeholder_once():
    files = {"frontend/src/pages/P.tsx": "import s from './P.module.css'\nimport '../styles/global.css'\nimport x from './x'\n",
             "frontend/src/styles/global.css": "body{}"}
    created = reconcile_missing_styles(files)
    assert list(created) == ["frontend/src/pages/P.module.css"] and "Placeholder" in created["frontend/src/pages/P.module.css"]
    assert reconcile_missing_styles({**files, **created}) == {}
    assert "frontend/src/pages/P.module.css" in reconcile_wiring({**files, "frontend/package.json": "{}"})


# --------------------------------------------------------------- relevance and caps

def test_declarative_code_and_ui_are_not_worth_a_unit_test():
    for path in ("backend/app/models/vehicle.py", "backend/app/modules/auth/schemas.py", "backend/app/config.py",
                 "backend/app/database.py", "backend/app/__init__.py", "backend/app/main.py",
                 "frontend/src/components/ui/Button.tsx", "frontend/src/pages/DashboardPage.tsx",
                 "frontend/src/types/auth.ts", "frontend/src/hooks/useAuth.ts", "frontend/src/services/apiClient.ts"):
        assert unit_test_cap(path) == 0, path
        assert not _testable(path, "def f():\n    pass\nexport function f() {}\n"), path


def test_behaviour_is_capped_by_what_it_is():
    caps = {p: unit_test_cap(p) for p in ("backend/app/modules/auth/router.py", "backend/app/modules/auth/service.py",
                                     "backend/app/repositories/user_repository.py", "backend/app/middleware/rbac.py",
                                     "backend/app/utils/security.py", "backend/src/routes/login.js",
                                     "frontend/src/utils/dateUtils.ts")}
    assert caps == {"backend/app/modules/auth/router.py": 8, "backend/app/modules/auth/service.py": 8,
                    "backend/app/repositories/user_repository.py": 4, "backend/app/middleware/rbac.py": 6,
                    "backend/app/utils/security.py": 6, "backend/src/routes/login.js": 8, "frontend/src/utils/dateUtils.ts": 5}


TESTS = '''import pytest


@pytest.fixture
def client():
    return 1


def test_a(client):
    assert client


@pytest.mark.parametrize("v", [1, 2])
def test_b(v):
    assert v


class TestRouter:
    def test_c(self):
        assert 1

    async def test_d(self):
        assert 1


class TestLater:
    def test_e(self):
        assert 1
'''


def _names(src: str) -> list[str]:
    return [n.name for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def test_the_trim_keeps_the_first_n_tests_and_stays_valid_python():
    assert trim_python_tests(TESTS, 10) == TESTS
    two = trim_python_tests(TESTS, 2)
    assert [n for n in _names(two) if n.startswith("test")] == ["test_a", "test_b"] and "client" in _names(two)
    assert "@pytest.mark.parametrize" in two and "TestRouter" not in two and "TestLater" not in two
    three = trim_python_tests(TESTS, 3)
    assert "test_c" in three and "test_d" not in three


def test_a_file_that_does_not_parse_is_left_alone():
    assert trim_python_tests("def test_x(:\n", 1) == "def test_x(:\n"


def test_the_agent_enforces_the_cap_even_when_the_model_ignores_it():
    many = "\n\n".join(f"def test_{n}():\n    assert {n} >= 0" for n in range(40)) + "\n"
    llm = FakeLLMGateway(lambda prompt: json.dumps({"files": [{"path": "backend/tests/test_modules_auth_router.py",
                                                              "content": many}]}))
    item = WorkItem(id="backend-auth", target_files=["backend/app/__init__.py", "backend/app/modules/auth/router.py"])
    executor = FakeExecutor(files={"p/backend/app/__init__.py": "",
                                   "p/backend/app/modules/auth/router.py": "def login():\n    return {}\n"})
    state = new_state(run_id="r", attempt=1, project_id="p", design_package={}, work_items=[item])
    out = UnitTestAgent(executor=executor, llm=llm).execute(state)
    written = executor.files[out["unit_tests"][0]]
    assert len([n for n in _names(written) if n.startswith("test_")]) == 8
    assert "AT MOST 8 focused test functions" in llm.calls[0]["prompt"]
