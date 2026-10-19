"""Two defects seen in a real Fleet run (17 debugging rounds).

* Rounds 10-13 wrote nothing: the model's reply hit ``max_tokens`` mid-analysis, the tool loop
  returned it as the "answer", and the same test file was re-attempted four times. A cut-off turn is
  now nudged (small ``edit_file`` calls, no repeated analysis) instead of returned, and a tool call
  whose input was cut mid-way is NOT run.
* Frontend hook/service tests (152 of them) were generated although only pure utilities earn a unit
  test: the rule looked at ``frontend/...`` but the agent passes ``<project>/frontend/...``.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.agents.unit_test import _testable, unit_test_cap
from app.services import llm_gateway as gw_module
from app.services.boilerplate import render_scaffold
from app.services.llm_gateway import LLMGateway


class _Client:
    def __init__(self, script):
        self.script, self.sent = list(script), []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.sent.append([dict(m) for m in kwargs["messages"]])
        return self.script.pop(0)


def _text(text, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def _tool(stop, call_id="t1"):
    block = SimpleNamespace(type="tool_use", name="edit_file", input={"path": "a", "old": "x"}, id=call_id)
    return SimpleNamespace(content=[block], stop_reason=stop, usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def _gateway(client, monkeypatch, ran):
    gateway = LLMGateway()
    gateway._prompt_cache = False
    gateway._client = client
    monkeypatch.setattr(gw_module, "_with_rate_limit_retry", lambda fn, **_: fn())
    tool = SimpleNamespace(name="edit_file", description="d", input_schema={"type": "object"},
                           handler=lambda **kw: ran.append(kw) or "edited")
    return gateway, [tool]


def test_a_cut_off_analysis_is_nudged_not_returned(monkeypatch):
    ran = []
    client = _Client([_text("long analysis...", "max_tokens"), _tool("tool_use"), _text("done")])
    gateway, tools = _gateway(client, monkeypatch, ran)
    assert gateway.complete_with_tools("fix", tools=tools, max_iters=6) == "done"
    nudge = client.sent[1][-1]
    assert nudge["role"] == "user" and "CUT OFF" in nudge["content"] and "edit_file" in nudge["content"]
    assert len(ran) == 1                                                  # the later, complete call did run


def test_a_tool_call_cut_off_mid_way_is_not_run(monkeypatch):
    ran = []
    client = _Client([_tool("max_tokens"), _text("done")])
    gateway, tools = _gateway(client, monkeypatch, ran)
    gateway.complete_with_tools("fix", tools=tools, max_iters=4)
    assert ran == []
    result = client.sent[1][-1]["content"][0]
    assert result["is_error"] and result["tool_use_id"] == "t1" and "Not run" in result["content"]


def test_repeated_cut_offs_are_eventually_returned_as_they_are(monkeypatch):
    client = _Client([_text("a", "max_tokens"), _text("b", "max_tokens"), _text("c", "max_tokens")])
    gateway, tools = _gateway(client, monkeypatch, [])
    assert gateway.complete_with_tools("fix", tools=tools, max_iters=6) == "c"      # 2 nudges, then give up
    assert len(client.sent) == 3


# --------------------------------------------------------------- frontend test relevance

def test_frontend_hooks_services_and_router_are_not_unit_tested_with_or_without_the_project_prefix():
    for prefix in ("", "fleet_1/"):
        for path in ("frontend/src/hooks/useAuth.ts", "frontend/src/services/authService.ts",
                     "frontend/src/router/routes.ts", "frontend/src/context/AuthContext.tsx"):
            assert unit_test_cap(prefix + path) == 0, prefix + path
        assert unit_test_cap(prefix + "frontend/src/utils/dateUtils.ts") == 5
        assert unit_test_cap(prefix + "backend/app/modules/auth/router.py") == 8


def test_a_workspace_path_is_judged_like_a_project_path():
    assert not _testable("fleet_1/frontend/src/hooks/useAuth.ts", "export function useAuth() { return 1 }")
    assert _testable("fleet_1/frontend/src/utils/dateUtils.ts", "export function fmt(d) { return d }")


def test_the_scaffold_installs_jsdom_with_vitest():
    pkg = next(f["content"] for f in render_scaffold("p") if f["path"] == "package.json")
    assert '"vitest"' in pkg and '"jsdom"' in pkg
