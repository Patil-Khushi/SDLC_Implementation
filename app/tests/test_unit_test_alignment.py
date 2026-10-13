"""Unit-test generation, aligned with the rest of the pipeline.

One test file per call (a many-file module can no longer truncate a single reply), each test file at
a path unique to its source, the naming contract / module map / requirement text / validation rules
/ sample payloads in the prompt, the test environment stated (TestClient + dependency_overrides, a
database that unit tests must not depend on), and nothing asked for code with no behaviour.
"""

from __future__ import annotations

import json

from app.agents.unit_test import UnitTestAgent, _suggested_test_path, _testable
from app.graph.state import new_state
from app.integrations.executor import FakeExecutor
from app.models import WorkItem
from app.services.llm_gateway import FakeLLMGateway

ROUTER = "from fastapi import APIRouter\nrouter = APIRouter()\n\n\n@router.post('/login')\ndef login():\n    return {}\n"
SERVICE = "def authenticate(email, password):\n    return True\n"

PACK = {
    "openapi.json": {"openapi": "3.0.3", "paths": {"/auth/login": {"post": {
        "operationId": "login", "responses": {"200": {"description": "ok"}}}}}},
    "validation-rules.json": {"login": {"email": {"message": "Enter a valid email address."}}},
    "extracted_requirements.json": {"functional": [{"id": "REQ-1", "text": "A user can sign in with email and password."}]},
}
FILES = ["backend/app/__init__.py", "backend/app/main.py", "backend/app/modules/auth/__init__.py",
         "backend/app/modules/auth/router.py", "backend/app/modules/auth/service.py"]
ITEM = WorkItem(id="backend-modules-auth", requirement_ids=["REQ-1"], endpoints=["POST /auth/login"],
                target_files=FILES[2:])


def _reply(prompt: str) -> str:
    path = prompt.rsplit("at: ", 1)[-1].split("\n", 1)[0].strip()
    return json.dumps({"files": [{"path": path, "content": "def test_x():\n    assert True\n"}]})


def _run(llm: FakeLLMGateway, items=None):
    executor = FakeExecutor(files={
        "p/backend/app/modules/auth/__init__.py": "", "p/backend/app/modules/auth/router.py": ROUTER,
        "p/backend/app/modules/auth/service.py": SERVICE, "p/backend/app/main.py": "app = 1\n",
    })
    state = new_state(run_id="r", attempt=1, project_id="p", design_package=PACK,
                      work_items=items or [WorkItem(id="backend-root", target_files=FILES[:2]), ITEM])
    return UnitTestAgent(executor=executor, llm=llm).execute(state), executor


def test_one_call_per_testable_file_each_at_its_own_path():
    llm = FakeLLMGateway(_reply)
    out, executor = _run(llm)
    assert len(llm.calls) == 2                                        # router + service; the empty __init__ is skipped
    assert out["unit_tests"] == ["p/backend/tests/test_modules_auth_router.py",
                                 "p/backend/tests/test_modules_auth_service.py"]
    assert out["tests_ok"] is True


def test_the_prompt_carries_the_contract_requirements_rules_and_environment():
    llm = FakeLLMGateway(_reply)
    _run(llm)
    prompt = llm.calls[0]["prompt"]
    assert prompt.startswith("NAMING CONTRACT") and "MODULE MAP" in prompt and "app.modules.auth: router, service" in prompt
    assert "A user can sign in with email and password." in prompt
    assert "Enter a valid email address." in prompt
    assert "app.dependency_overrides[" in prompt and "from app.main import app" in prompt
    assert "ONE self-contained test file" in prompt
    assert prompt.rstrip().endswith("Respond with STRICT JSON only, containing exactly one file: "
                                    '{"files":[{"path":...,"content":...}],"notes":...}')
    prefix = llm.calls[0]["kwargs"]["cache_prefix"]
    assert len(prefix) == 2 and prefix[0].startswith("NAMING CONTRACT")      # shared, then this item's


def test_a_database_the_tests_must_not_depend_on_is_stated():
    class Facts(FakeExecutor):
        def environment_facts(self):
            return {"database": "UNAVAILABLE here (no Docker)"}

    llm = FakeLLMGateway(_reply)
    state = new_state(run_id="r", attempt=1, project_id="p", design_package=PACK, work_items=[ITEM])
    executor = Facts(files={"p/backend/app/modules/auth/router.py": ROUTER})
    UnitTestAgent(executor=executor, llm=llm).execute(state)
    assert "Database: UNAVAILABLE here (no Docker)" in llm.calls[0]["prompt"]


def test_a_bad_reply_is_retried_once_for_that_file_only():
    replies = iter(["not json", _reply("at: backend/tests/test_modules_auth_router.py"), _reply("at: z")])
    llm = FakeLLMGateway(lambda prompt: next(replies))
    out, _ = _run(llm)
    assert len(llm.calls) == 3 and len(out["unit_tests"]) == 2


def test_an_item_with_nothing_to_test_is_not_a_failure():
    types_only = WorkItem(id="frontend-types", target_files=["frontend/src/types/auth.ts"])
    state = new_state(run_id="r", attempt=1, project_id="p", design_package={}, work_items=[types_only])
    llm = FakeLLMGateway([])
    executor = FakeExecutor(files={"p/frontend/src/types/auth.ts": "export interface User { id: string }\n"})
    out = UnitTestAgent(executor=executor, llm=llm).execute(state)
    assert llm.calls == [] and out["tests_ok"] is True
    assert "nothing to test" in out["generation_summary"]


def test_what_is_testable():
    assert _testable("backend/app/service.py", SERVICE)
    assert not _testable("backend/app/__init__.py", "")
    assert not _testable("backend/app/config.py", "# only a comment\n")
    assert not _testable("backend/alembic/versions/0001_initial.py", "def upgrade():\n    pass\n")
    assert not _testable("frontend/src/types/user.d.ts", "export {}")
    assert not _testable("frontend/vite.config.ts", "export default defineConfig({})")
    assert not _testable("frontend/src/main.tsx", "ReactDOM.createRoot(el).render(<App />)\n")
    assert _testable("frontend/src/utils/date.ts", "export function fmt(d) { return d }\n")
    assert not _testable("backend/tests/test_x.py", "def test_x():\n    pass\n")


def test_test_paths_are_unique_per_source_and_importable():
    paths = ["backend/app/__init__.py", "backend/app/modules/auth/__init__.py",
             "backend/app/modules/auth/router.py", "backend/app/modules/depots/router.py"]
    a = _suggested_test_path("backend/app/modules/auth/router.py", paths)
    b = _suggested_test_path("backend/app/modules/depots/router.py", paths)
    assert (a, b) == ("backend/tests/test_modules_auth_router.py", "backend/tests/test_modules_depots_router.py")
    assert _suggested_test_path("frontend/src/utils/date.ts", []) == "frontend/src/utils/date.test.ts"
