"""Debugging, part 2: what each round is grounded in, what it works on, and what it remembers.

* The project's binding context (naming contract, module map, conventions) reaches every round —
  as the CACHEABLE prefix, because a tool session re-sends its whole conversation on every turn.
* Each round works a focused BATCH drawn across the failing checks, boot first: worst-first over the
  whole failure handed a round twelve TypeScript files while the one backend file that stopped the
  app from starting sat at position 40.
* Each round is told what earlier rounds changed and whether the count went down, and a file edited
  round after round without progress is called out as a dead end.
* Facts about the environment and the reconcile pass's cross-file findings are in view.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

from app.agents.debugging import DebuggingAgent, _batch, _dead_ends
from app.graph import router
from app.graph.state import new_state
from app.integrations.executor import FakeExecutor
from app.models import WorkItem
from app.services import llm_gateway as gw_module
from app.services.llm_gateway import FakeLLMGateway, LLMGateway, complete_with_tools_cached

TSC = "\n".join(f"src/pages/P{n}.tsx({n},1): error TS2322: Type 'x' is not assignable." for n in range(1, 16))
BOOT = r'''Traceback (most recent call last):
  File "C:\venvs\b\Lib\site-packages\uvicorn\importer.py", line 19, in import_from_string
    module = importlib.import_module(module_str)
  File "C:\ws\proj\backend\app\main.py", line 5, in <module>
    from app.modules.auth.router import router
  File "C:\ws\proj\backend\app\repositories\__init__.py", line 7, in <module>
    from app.repositories.odometer_repository import OdometerRepository
ModuleNotFoundError: No module named 'app.repositories.odometer_repository'
'''


def _failing() -> list[dict[str, Any]]:
    return [
        {"name": "compile", "passed": False, "stderr": "[tsc[proj/frontend]] " + TSC, "stdout": ""},
        {"name": "boot", "passed": False, "stderr": "[boot[proj/backend]] " + BOOT, "stdout": ""},
    ]


# --------------------------------------------------------------- the batch

def test_the_batch_starts_with_the_file_that_stops_the_app_from_starting():
    batch = _batch(_failing(), "proj")
    path, _n, lines = batch[0]
    assert path == "backend/app/repositories/__init__.py"
    assert lines[-1] == "ModuleNotFoundError: No module named 'app.repositories.odometer_repository'"
    assert batch[1][0].startswith("frontend/src/pages/")           # then the next check's worst
    assert len(batch) == 12 and len({p for p, _n, _l in batch}) == 12


def test_each_batch_file_carries_its_own_error_lines():
    batch = dict((p, lines) for p, _n, lines in _batch(_failing(), "proj"))
    assert batch["frontend/src/pages/P3.tsx"] == ["src/pages/P3.tsx(3,1): error TS2322: Type 'x' is not assignable."]


def test_frames_a_traceback_only_passed_through_are_not_batched():
    check = {"name": "boot", "passed": False, "stderr": "[boot[proj/backend]] " + BOOT}
    assert [p for p, _n, _l in _batch([check], "proj")] == ["backend/app/repositories/__init__.py"]   # not main.py


def test_the_full_output_is_trimmed_once_the_batch_carries_the_errors():
    long_tsc = "\n".join(f"src/pages/P{n}.tsx({n},1): error TS2322: {'x' * 80}" for n in range(1, 400))
    state = _state()
    state["debug_result"] = {"passed": False, "checks": [{"name": "compile", "passed": False,
                                                           "stderr": "[tsc[proj/frontend]] " + long_tsc}]}
    llm = FakeLLMGateway([_fix("x.py")])
    DebuggingAgent(executor=FakeExecutor(), llm=llm).execute(state)
    output = llm.calls[0]["prompt"].split("Output of the failing `compile` check:", 1)[1]
    assert "characters omitted" in output and len(output) < 12000


def test_a_small_failure_is_one_small_batch():
    check = {"name": "compile", "passed": False, "stderr": "[tsc[proj/frontend]] src/a.ts(1,1): error TS1: x"}
    assert [p for p, _n, _l in _batch([check], "proj")] == ["frontend/src/a.ts"]


# --------------------------------------------------------------- a round, end to end

PACK = {
    "skills.md": "\n".join(f"- Always follow project rule {n}; never break it." for n in range(9)),
    "db_schema.sql": "CREATE TABLE users (\n  id uuid PRIMARY KEY,\n  email text NOT NULL\n);\n",
    "openapi.json": {"openapi": "3.0.3", "paths": {"/auth/login": {"post": {"operationId": "login",
                                                                          "responses": {"200": {"description": "ok"}}}}}},
}


class _FactsExecutor(FakeExecutor):
    def environment_facts(self) -> dict[str, str]:
        return {"Python for backend": "3.11 (the project's own version)", "database": "UNAVAILABLE here (no Docker)"}


def _state(**extra: Any) -> dict[str, Any]:
    state = new_state(run_id="r", attempt=1, project_id="proj", design_package=PACK)
    state["debug_result"] = {"passed": False, "checks": _failing()}
    state["generated_code"] = ["proj/backend/app/main.py"]
    state["work_items"] = [WorkItem(id="backend-root", target_files=["backend/app/__init__.py", "backend/app/main.py"])]
    state.update(extra)
    return state


def _fix(path: str) -> str:
    return json.dumps({"files": [{"path": path, "content": "x = 1\n"}], "notes": f"fixed {path}"})


def test_the_project_context_is_the_cacheable_prefix_of_every_round():
    llm = FakeLLMGateway([_fix("backend/app/repositories/__init__.py")])
    DebuggingAgent(executor=FakeExecutor(), llm=llm).execute(_state())
    prefix = llm.calls[0]["kwargs"]["cache_prefix"]
    assert prefix[0].startswith("NAMING CONTRACT") and "MODULE MAP" in prefix[0]
    assert prefix[1].startswith("## Conventions") and "project rule 0" in prefix[1]
    assert "THIS ROUND: fix these" in llm.calls[0]["prompt"]


def test_environment_facts_unverified_checks_and_cross_file_findings_are_in_view():
    llm = FakeLLMGateway([_fix("x.py")])
    state = _state(unverified_checks=[{"check": "test", "reason": "pytest: no database"}],
                   integration_findings=[{"kind": "base_path", "detail": "routes are not under /v1", "files": []}])
    DebuggingAgent(executor=_FactsExecutor(), llm=llm).execute(state)
    prompt = llm.calls[0]["prompt"]
    assert "- Python for backend: 3.11 (the project's own version)" in prompt
    assert "- database: UNAVAILABLE here (no Docker)" in prompt
    assert "- NOT verified here: test - pytest: no database" in prompt
    assert "- [base_path] routes are not under /v1" in prompt


def test_each_round_learns_whether_the_previous_one_helped():
    agent = DebuggingAgent(executor=FakeExecutor(), llm=FakeLLMGateway([_fix("backend/app/a.py"), _fix("backend/app/b.py")]))
    state = agent.execute(_state())
    assert state["debug_history"][-1]["after"] is None                       # not measured yet
    fewer = [{"name": "compile", "passed": False, "stderr": "[tsc[proj/frontend]] " + "\n".join(TSC.splitlines()[:5])}]
    state["debug_result"] = {"passed": False, "checks": fewer}
    state = agent.execute(state)
    first = state["debug_history"][0]
    assert first["files"] == ["backend/app/a.py"] and first["after"] == 5 and first["progress"] is True
    prompt = agent.llm.calls[1]["prompt"]
    assert "Earlier rounds" in prompt and "changed 1 file(s) (backend/app/a.py)" in prompt and "(progress)" in prompt


def test_a_file_edited_round_after_round_without_progress_is_called_out():
    history = [{"round": n, "kind": "compile/build", "before": 10, "after": 10, "progress": False,
                "files": ["frontend/src/Button.tsx"], "notes": ""} for n in (1, 2, 3)]
    assert _dead_ends(history) == ["frontend/src/Button.tsx"]
    llm = FakeLLMGateway([_fix("x.py")])
    DebuggingAgent(executor=FakeExecutor(), llm=llm).execute(_state(debug_history=history))
    assert "Edited in 3+ rounds without the count dropping: frontend/src/Button.tsx" in llm.calls[0]["prompt"]


def test_a_gateway_without_cache_prefix_gets_the_context_inline():
    seen: list[str] = []

    class Old(LLMGateway):
        def __init__(self) -> None:  # noqa: D401
            self.last_stop_reason = None

        def complete_with_tools(self, prompt: str, *, system=None, tools=None, max_iters: int = 6) -> str:
            seen.append(prompt)
            return _fix("x.py")

    out = complete_with_tools_cached(Old(), "TASK", cache_prefix=["CONTEXT"], system="s", tools=[1], max_iters=2)
    assert seen == ["CONTEXT\n\nTASK"] and json.loads(out)["files"]


def test_the_stall_cap_is_lower_now_that_progress_is_measured():
    assert router.DEBUG_CAP == 5


# --------------------------------------------------------------- the tool loop's cache

class _Client:
    """messages.create scripted: a tool call per turn for ``turns`` turns, then a text reply."""

    def __init__(self, turns: int, fail_first: Exception | None = None) -> None:
        self.sent: list[list[dict]] = []
        self.turns = turns
        self.fail_first = fail_first
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.sent.append(copy.deepcopy(kwargs["messages"]))
        if self.fail_first is not None:
            exc, self.fail_first = self.fail_first, None
            raise exc
        usage = SimpleNamespace(input_tokens=1, output_tokens=1)
        if len(self.sent) <= self.turns:
            block = SimpleNamespace(type="tool_use", name="read_file", input={"path": "a"}, id=f"t{len(self.sent)}")
            return SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="done")], stop_reason="end_turn", usage=usage)


def _gateway(client: _Client, monkeypatch) -> LLMGateway:
    gateway = LLMGateway()
    gateway._prompt_cache = True
    gateway._client = client
    monkeypatch.setattr(gw_module, "_with_rate_limit_retry", lambda fn, **_: fn())
    return gateway


_TOOL = SimpleNamespace(name="read_file", description="read", input_schema={"type": "object"}, handler=lambda **kw: "content")


def _breakpoints(messages: list[dict]) -> int:
    return sum(1 for m in messages if isinstance(m["content"], list)
               for b in m["content"] if isinstance(b, dict) and "cache_control" in b)


def test_the_tool_loop_caches_the_prefix_and_a_rolling_breakpoint(monkeypatch):
    client = _Client(turns=3)
    assert _gateway(client, monkeypatch).complete_with_tools(
        "TASK", tools=[_TOOL], max_iters=6, cache_prefix=["SHARED", "ITEM"]) == "done"
    first = client.sent[0][0]["content"]
    assert [b.get("cache_control") is not None for b in first] == [True, True, False]
    last = client.sent[-1]
    assert _breakpoints(last) == 3                                   # 2 in the prefix + 1 rolling
    assert "cache_control" in last[-1]["content"][-1]                # on the NEWEST tool result


def test_a_deployment_that_rejects_cache_control_runs_the_loop_uncached(monkeypatch):
    rejection = anthropic.BadRequestError(
        "cache_control not supported", response=httpx.Response(400, request=httpx.Request("POST", "https://x.invalid")),
        body=None)
    client = _Client(turns=1, fail_first=rejection)
    gateway = _gateway(client, monkeypatch)
    assert gateway.complete_with_tools("TASK", tools=[_TOOL], max_iters=4, cache_prefix=["SHARED"]) == "done"
    assert client.sent[1][0]["content"] == "SHARED\n\nTASK"           # retried as plain text
    assert all(_breakpoints(sent) == 0 for sent in client.sent[2:])
    assert gateway._prompt_cache is False


@pytest.mark.parametrize("cache", [False])
def test_without_caching_the_first_message_is_one_string(monkeypatch, cache):
    client = _Client(turns=0)
    gateway = _gateway(client, monkeypatch)
    gateway._prompt_cache = cache
    gateway.complete_with_tools("TASK", tools=[_TOOL], cache_prefix=["SHARED"])
    assert client.sent[0][0]["content"] == "SHARED\n\nTASK"
