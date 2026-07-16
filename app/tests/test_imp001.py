"""IMP-001 end-to-end suite — every generation/repair test compiles for REAL in the exec-sandbox.

No fakes on the happy path: the real ``mcp_executor`` runs the real compiler; LLM responses are
recorded once (``RECORD=1``) then replayed (deterministic, zero tokens). The failure-path tests
sequence hand-authored recordings (``.broken1`` / ``.fixed`` / ``.badjson``) so the REAL compiler
fails then passes — the compile step is always the sandbox.

Tests 1–5 are ``@integration`` and skip cleanly when ``SANDBOX_MCP_URL`` is absent / unreachable.
Test 6 (manifest gate) is a pure disk check and runs under ``pytest -m "not integration"``.
"""

from __future__ import annotations

import os
import re
from typing import Any

import pytest
from langgraph.types import Command

from app.graph.graph import workflow
from app.graph.router import REPAIR_CAP
from app.graph.state import WorkflowState, new_state
from app.integrations.executor import set_executor
from app.models import WorkItem
from app.services import llm_gateway
from app.services.manifest_gate import check_manifest

integration = pytest.mark.integration
needs_sandbox = pytest.mark.skipif(
    not os.environ.get("SANDBOX_MCP_URL"),
    reason="SANDBOX_MCP_URL not set — exec-sandbox required for real-compile tests",
)


# --------------------------------------------------------------------------- helpers

def _item(plan: list[WorkItem], item_id: str) -> WorkItem:
    return next(i for i in plan if i.id == item_id)


def _run_item(
    item: WorkItem,
    executor: Any,
    gateway: Any,
    design_package: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    project_id: str = "p1",
    attempt: int = 7,
) -> WorkflowState:
    """Drive the compiled graph for a single work item against the real executor + replay gateway.

    A single-item plan that gate-passes lands at ``batch_review`` (paused, ``workflow_status ==
    "pending_review"``, no commit yet) — auto-approve so callers see the same all-pass/commit
    outcome as before batch review existed. Escalating runs never reach that status, so this is
    a no-op for them.
    """
    monkeypatch.setattr(llm_gateway.llm_gateway, "complete", gateway.complete)
    monkeypatch.setattr(llm_gateway.llm_gateway, "complete_with_tools", gateway.complete_with_tools)
    set_executor(executor)
    initial = new_state(
        run_id="run-1", attempt=attempt, project_id=project_id,
        design_package=design_package, work_items=[item],
    )
    config = {"configurable": {"thread_id": f"t-{item.id}"}, "recursion_limit": 100}
    try:
        workflow.invoke(initial, config)
        state = dict(workflow.get_state(config).values)
        if state.get("workflow_status") == "pending_review":
            workflow.invoke(Command(resume={"approved": True}), config)
            state = dict(workflow.get_state(config).values)
        return state  # type: ignore[return-value]
    finally:
        set_executor(None)


def _commit_count(executor: Any, project_id: str = "p1") -> int:
    """Number of commits in the sandbox project (0 when the repo has no commits yet)."""
    result = executor.run_command(["git", "rev-list", "--count", "HEAD"], project_id)
    try:
        return int(result.stdout.strip())
    except (ValueError, AttributeError):
        return 0


# --------------------------------------------------------------------------- 1. backend happy path

@integration
@needs_sandbox
def test_login_backend_happy_path(dummy_plan, design_package, mcp_executor, fake_gateway, monkeypatch) -> None:
    item = _item(dummy_plan, "backend-loginUser")  # POST /auth/login, loginUser, REQ-002
    state = _run_item(item, mcp_executor, fake_gateway, design_package, monkeypatch)

    assert state["generated_code"], "files were written into the sandbox workspace"
    assert state["gate_result"] is not None and state["gate_result"]["passed"] is True  # completeness gate passed
    assert _commit_count(mcp_executor) == 1  # commit landed after completeness + auto-approval

    summary = state["generation_summary"]
    assert "REQ-002" in summary and "/auth/login" in summary
    assert state["generation_metrics"]["files_produced"] >= 1
    assert state["run_id"] == "run-1" and state["attempt"] == 7  # echoed unchanged

    body = "\n".join(mcp_executor.read_file(p) for p in state["generated_code"])
    assert "Invalid email or password." in body  # 401 message verbatim from validation-rules.json


# --------------------------------------------------------------------------- 2. frontend happy path

@integration
@needs_sandbox
def test_login_frontend_happy_path(dummy_plan, design_package, mcp_executor, fake_gateway, monkeypatch) -> None:
    item = _item(dummy_plan, "frontend-login")  # route login, REQ-002
    state = _run_item(item, mcp_executor, fake_gateway, design_package, monkeypatch)

    assert state["generated_code"]
    assert state["gate_result"] is not None and state["gate_result"]["passed"] is True  # completeness gate passed
    assert _commit_count(mcp_executor) == 1

    body = "\n".join(mcp_executor.read_file(p) for p in state["generated_code"])
    assert _uses_design_tokens(design_package, body)          # generated code uses tokens.json
    assert _any_validation_message(design_package, body)      # verbatim validation messages present


# --------------------------------------------------------------------------- 3. repair loop (missing file -> supplied)

class _MissingFileGateway:
    """Inline gateway: codegen omits the LAST target file; repair supplies it (or never, at the cap).

    Drives the completeness gate against the REAL sandbox disk — no recordings, no compiler.
    """

    def __init__(self, item: WorkItem, *, project_id: str = "p1", ever_supply: bool = True) -> None:
        self._item = item
        self._pid = project_id
        self._ever = ever_supply

    @staticmethod
    def _stub(path: str) -> str:
        return f"export const x = 1; // {path}\n" if path.endswith((".ts", ".tsx")) else f"# {path}\n"

    def complete(self, prompt: str, *, system: str | None = None, **_kw: Any) -> str:
        # write every target EXCEPT the last one -> completeness gate fails
        files = [{"path": p, "content": self._stub(p)} for p in self._item.target_files[:-1]]
        return json.dumps({"files": files, "notes": ""})

    def complete_with_tools(self, prompt: str, *, system: str | None = None, tools: Any = None, max_iters: int = 4) -> str:
        if not self._ever:
            return json.dumps({"files": [], "notes": "no fix"})
        missing = self._item.target_files[-1]  # repair writes the prefixed on-disk path directly
        return json.dumps({"files": [{"path": f"{self._pid}/{missing}", "content": self._stub(missing)}], "notes": "fixed"})


@integration
@needs_sandbox
def test_repair_loop_missing_file_then_supplied(dummy_plan, design_package, mcp_executor, monkeypatch) -> None:
    item = _item(dummy_plan, "backend-loginUser")
    gateway = _MissingFileGateway(item, ever_supply=True)

    state = _run_item(item, mcp_executor, gateway, design_package, monkeypatch)

    # codegen (incomplete) -> gate(fail: file missing) -> repair (supplies it) -> gate(pass) -> commit
    assert state["repair_attempt"] == 1
    assert state["gate_result"] is not None and state["gate_result"]["passed"] is True
    assert _commit_count(mcp_executor) == 1


# --------------------------------------------------------------------------- 4. cap (file never supplied)

@integration
@needs_sandbox
def test_cap_file_never_supplied(dummy_plan, design_package, mcp_executor, monkeypatch) -> None:
    item = _item(dummy_plan, "backend-loginUser")
    gateway = _MissingFileGateway(item, ever_supply=False)  # repair never conjures the missing file

    state = _run_item(item, mcp_executor, gateway, design_package, monkeypatch)

    assert state["workflow_status"] == "needs_human_review"
    assert state["repair_attempt"] == REPAIR_CAP
    assert _commit_count(mcp_executor) == 0  # NO commit on escalation


# --------------------------------------------------------------------------- 5. bad JSON (no files, no commit)

@integration
@needs_sandbox
def test_bad_json_records_failure_no_commit(dummy_plan, design_package, mcp_executor, fake_gateway, monkeypatch) -> None:
    item = _item(dummy_plan, "backend-loginUser")
    fake_gateway.use("backend-loginUser.badjson")  # invalid JSON served for both attempts

    state = _run_item(item, mcp_executor, fake_gateway, design_package, monkeypatch)

    assert state["generated_code"] == []             # no files written
    assert "FAILED" in state["generation_summary"]   # item recorded as failed
    assert _commit_count(mcp_executor) == 0           # no commit
    assert state["workflow_status"] == "needs_human_review"


# --------------------------------------------------------------------------- 6. manifest gate (pure disk check)

def test_manifest_gate_checks_disk_not_claims(dummy_pack_complete, dummy_pack_missing) -> None:
    assert check_manifest(dummy_pack_complete) == {"ok": True, "missing": []}

    result = check_manifest(dummy_pack_missing)
    assert result["ok"] is False
    assert result["missing"] == ["D1", "D2"]  # schema.sql + openapi.yaml

    # the decoy manifest asserts 20/20; the gate ignores the claim and trusts the disk
    assert (dummy_pack_missing / "index.false-claim.md").exists()


# --------------------------------------------------------------------------- verbatim/token helpers

def _validation_messages(design_package: dict[str, Any]) -> list[str]:
    rules = design_package.get("validation-rules.json")
    messages: list[str] = []

    def _collect(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("message", "msg") and isinstance(value, str):
                    messages.append(value)
                else:
                    _collect(value)
        elif isinstance(node, list):
            for value in node:
                _collect(value)

    _collect(rules)
    return messages


def _any_validation_message(design_package: dict[str, Any], body: str) -> bool:
    messages = _validation_messages(design_package)
    return any(msg and msg in body for msg in messages)


_TOKEN_META_KEYS = {"artifact", "handoffId", "owner", "version", "conventions"}
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{3,8}$")
_DIM_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:px|rem|em)$")
# Tailwind / CSS-var prefixes a token *name* appears behind when actually used.
_TOKEN_USE_PREFIXES = ("bg-", "text-", "border-", "ring-", "fill-", "stroke-", "--", "var(--")


def _uses_design_tokens(design_package: dict[str, Any], body: str) -> bool:
    """True only on a MEANINGFUL sign of token use — not a bare substring of a generic word.

    Signals (any one): a token-shaped VALUE used literally (hex / px|rem|em); a distinctive
    token NAME used behind a Tailwind/CSS-var prefix (e.g. `bg-primary`); or two+ distinct
    long token names present. This avoids both the old false-fail (checking group names like
    "color") and a false-pass (matching `error`/`text`/`min` in any TS file).
    """
    tokens = design_package.get("tokens.json")
    if not isinstance(tokens, dict):
        return False

    values: set[str] = set()  # token-shaped leaf values (hex / dimensions)
    names: set[str] = set()   # meaningful leaf names

    def _collect(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.startswith("_") or key in _TOKEN_META_KEYS:
                    continue
                if isinstance(value, str):
                    if _HEX_RE.match(value) or _DIM_RE.match(value):
                        values.add(value)
                    if len(key) >= 5:  # distinctive names only ('primary','surface','error',...)
                        names.add(key)
                else:
                    _collect(value)

    _collect(tokens)

    if any(value in body for value in values):
        return True  # a design-token value used literally (hex / dimension)
    if any(f"{prefix}{name}" in body for name in names for prefix in _TOKEN_USE_PREFIXES):
        return True  # a token name used behind a Tailwind/CSS-var prefix
    return sum(1 for name in names if name in body) >= 2  # multiple distinct token names present
