"""What each generation call is grounded in, and how it is sent.

Before this, a Fleet backend item's prompt had no conventions, no table definitions and no
request/response schemas (the lookups expected ``SKILL.md`` / ``schema.sql`` and the OpenAPI YAML
was line-filtered), and every page of a multi-page item got the same screen-name-filtered mockup
lines. Now the context comes in three tiers — shared by every item of a side, shared by an item's
files, and one file's own — and the first two travel as the gateway's cacheable prefix, because
each of ~200 files per run is its own call.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.agents.code_generator import CodeGeneratorAgent
from app.graph.state import new_state
from app.integrations.executor import FakeExecutor
from app.models import WorkItem
from app.services import llm_gateway as gw_module
from app.services.llm_gateway import FakeLLMGateway, LLMGateway, _cached_user_content, full_prompt
from app.services.pack_index import load_pack
from app.services.plan_builder import build_plan

_REPO = Path(__file__).resolve().parents[2]
PACKS = [_REPO / "uploads" / "v1", _REPO / "fixtures" / "resources", _REPO / "uploads" / "authentication"]


def _reply_for(prompt: str) -> str:
    path = prompt.rsplit("Generate EXACTLY ONE file now: ", 1)[-1].split("\n", 1)[0].strip()
    return json.dumps({"files": [{"path": path, "content": f"// {path}\n"}]})


# --------------------------------------------------------------- the real packs

@pytest.mark.parametrize("pack", PACKS, ids=lambda p: p.name)
def test_every_backend_item_gets_conventions_and_the_api_and_db_it_implements(pack: Path):
    if not pack.is_dir():
        pytest.skip(f"{pack} not present")
    package, plan, agent = load_pack(pack), build_plan(pack), CodeGeneratorAgent()
    backend = [i for i in plan if i.id.startswith("backend")]
    assert backend
    for item in backend:
        ctx = agent._build_context(item, package, [], work_items=plan)
        names = ctx.sections
        assert "Conventions" in names, item.id
        if item.endpoints:
            assert "API" in names and "(operationId:" in ctx.text, item.id
            assert "### Components" in ctx.text, item.id               # with the schemas it uses
        if item.tables:
            db = ctx.text.split("## DB", 1)[-1] if "DB" in names else ""
            assert "CREATE TABLE" in db or db.lstrip().startswith(("—", "TABLE")), item.id


@pytest.mark.skipif(not PACKS[0].is_dir(), reason="Fleet pack (uploads/v1) not present")
def test_each_fleet_page_is_built_from_its_own_screen():
    package, plan = load_pack(PACKS[0]), build_plan(PACKS[0])
    pages = next(i for i in plan if i.id == "frontend-pages")
    ctx = CodeGeneratorAgent()._build_context(pages, package, [], work_items=plan)
    login = next(p for p in pages.target_files if p.endswith("LoginPage.tsx"))
    dashboard = next(p for p in pages.target_files if p.endswith("DashboardPage.tsx"))
    assert 'id="screen-login"' in ctx.file_text(login) and 'id="screen-depot-dashboard"' not in ctx.file_text(login)
    assert 'id="screen-depot-dashboard"' in ctx.file_text(dashboard)
    assert "POST /auth/login" in ctx.file_text(login)                    # the endpoints this screen calls
    assert "Password must be at least 8 characters." in ctx.file_text(login)   # its form's rules
    assert 'id="screen-login"' not in ctx.text                         # not in the shared prefix


# --------------------------------------------------------------- how calls are sent

PACK = {
    "skills.md": "\n".join(f"- Always do rule {n}; never skip it." for n in range(10)),
    "openapi.json": {"openapi": "3.0.3", "paths": {"/login": {"post": {
        "operationId": "login", "responses": {"200": {"description": "ok"}}}}}},
    "schema.sql": "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);",
}


def _item(item_id: str, files: list[str], **kw) -> WorkItem:
    return WorkItem(id=item_id, target_files=files, **kw)


def _run(item: WorkItem, plan: list[WorkItem], llm) -> None:
    state = new_state(run_id="r", attempt=1, project_id="p", design_package=PACK)
    state["current_work_item"] = item
    state["work_items"] = plan
    CodeGeneratorAgent(executor=FakeExecutor(), llm=llm).execute(state)


def test_shared_and_item_context_travel_as_the_cacheable_prefix():
    plan = [_item("backend-a", ["backend/app/a.py", "backend/app/b.py"], endpoints=["POST /login"], tables=["users"]),
            _item("backend-c", ["backend/app/c.py", "backend/app/d.py"], tables=["users"])]
    llm = FakeLLMGateway(_reply_for)
    _run(plan[0], plan, llm)
    _run(plan[1], plan, llm)
    prefixes = [call["kwargs"]["cache_prefix"] for call in llm.calls]
    assert len(prefixes) == 4 and all(len(p) == 2 for p in prefixes)
    assert prefixes[0] == prefixes[1]                      # an item's files share the whole prefix
    assert prefixes[2] == prefixes[3]
    assert prefixes[0][0] == prefixes[2][0]                # items of one side share the first segment
    assert prefixes[0][1] != prefixes[2][1]
    assert prefixes[0][0].startswith("NAMING CONTRACT")
    assert "MODULE MAP" in prefixes[0][0] and "Always do rule 0" in prefixes[0][0]
    assert "(operationId: login)" in prefixes[0][1] and "CREATE TABLE users" in prefixes[0][1]


def test_an_item_with_nothing_of_its_own_sends_only_the_shared_segment():
    plan = [_item("backend-c", ["backend/app/c.py", "backend/app/d.py"])]
    llm = FakeLLMGateway(_reply_for)
    _run(plan[0], plan, llm)
    assert [len(call["kwargs"]["cache_prefix"]) for call in llm.calls] == [1, 1]     # empty segments dropped


def test_the_task_follows_the_context():
    plan = [_item("backend-a", ["backend/app/a.py", "backend/app/b.py"], endpoints=["POST /login"])]
    llm = FakeLLMGateway(_reply_for)
    _run(plan[0], plan, llm)
    prompt = llm.calls[0]["prompt"]                         # the whole message, as the model reads it
    assert prompt.index("NAMING CONTRACT") < prompt.index("Work item: backend-a") < prompt.index(
        "Generate EXACTLY ONE file now: backend/app/a.py")


def test_a_gateway_without_cache_prefix_gets_the_same_text_as_one_prompt():
    received: list[str] = []

    class OldGateway(LLMGateway):
        def __init__(self) -> None:  # noqa: D401 - no settings, no client
            pass

        def complete(self, prompt: str, *, system: str | None = None) -> str:   # predates cache_prefix
            received.append(prompt)
            return _reply_for(prompt)

    plan = [_item("backend-a", ["backend/app/a.py", "backend/app/b.py"], endpoints=["POST /login"])]
    _run(plan[0], plan, OldGateway())
    assert len(received) == 2
    assert received[0].startswith("NAMING CONTRACT") and "Generate EXACTLY ONE file now: backend/app/a.py" in received[0]


# --------------------------------------------------------------- the gateway

def test_full_prompt_and_cached_blocks_carry_the_same_text():
    blocks = _cached_user_content("TASK", ["SHARED", "", "ITEM"])
    assert [b.get("cache_control") for b in blocks] == [{"type": "ephemeral"}, {"type": "ephemeral"}, None]
    assert "".join(b["text"] for b in blocks) == "SHARED\n\nITEM\n\nTASK" == full_prompt("TASK", ["SHARED", "ITEM"])
    assert full_prompt("TASK") == "TASK" and _cached_user_content("TASK", None) == [{"type": "text", "text": "TASK"}]


class _FakeStream:
    def __init__(self, sink: list, fail_with: Exception | None) -> None:
        self._sink, self._fail = sink, fail_with

    def __call__(self, **kwargs):
        self._sink.append(kwargs)
        if self._fail is not None:
            exc, self._fail = self._fail, None
            raise exc
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        return SimpleNamespace(stop_reason="end_turn", usage=usage, content=[SimpleNamespace(type="text", text="ok")])


def _gateway(monkeypatch, *, cache: bool = True, fail_with: Exception | None = None) -> tuple[LLMGateway, list]:
    sent: list = []
    gateway = LLMGateway()
    gateway._prompt_cache = cache
    gateway._use_thinking = False
    gateway._client = SimpleNamespace(messages=SimpleNamespace(stream=_FakeStream(sent, fail_with)))
    monkeypatch.setattr(gw_module, "_with_rate_limit_retry", lambda fn, **_: fn())
    return gateway, sent


def test_the_gateway_marks_the_prefix_cacheable(monkeypatch):
    gateway, sent = _gateway(monkeypatch)
    assert gateway.complete("TASK", system="sys", cache_prefix=["SHARED", "ITEM"]) == "ok"
    content = sent[0]["messages"][0]["content"]
    assert [b.get("cache_control") for b in content] == [{"type": "ephemeral"}, {"type": "ephemeral"}, None]


def test_with_caching_off_the_prefix_is_sent_as_plain_text(monkeypatch):
    gateway, sent = _gateway(monkeypatch, cache=False)
    gateway.complete("TASK", cache_prefix=["SHARED"])
    assert sent[0]["messages"][0]["content"] == "SHARED\n\nTASK"


def test_a_deployment_that_rejects_cache_control_falls_back_for_good(monkeypatch):
    rejection = anthropic.BadRequestError(
        "cache_control is not supported on this deployment",
        response=httpx.Response(400, request=httpx.Request("POST", "https://example.invalid")), body=None,
    )
    gateway, sent = _gateway(monkeypatch, fail_with=rejection)
    assert gateway.complete("TASK", cache_prefix=["SHARED"]) == "ok"
    assert isinstance(sent[0]["messages"][0]["content"], list)          # the attempt that was refused
    assert sent[1]["messages"][0]["content"] == "SHARED\n\nTASK"         # the retry, same text
    gateway.complete("NEXT", cache_prefix=["SHARED"])
    assert sent[2]["messages"][0]["content"] == "SHARED\n\nNEXT"         # stays off


def test_an_unrelated_bad_request_is_not_swallowed(monkeypatch):
    other = anthropic.BadRequestError(
        "max_tokens too large", response=httpx.Response(400, request=httpx.Request("POST", "https://example.invalid")),
        body=None,
    )
    gateway, _sent = _gateway(monkeypatch, fail_with=other)
    with pytest.raises(anthropic.BadRequestError):
        gateway.complete("TASK", cache_prefix=["SHARED"])
