# SDLC Multi-Agent System — Development Context & Contracts

> **Purpose:** the single source of truth for building the Implementation service
> **in parallel**. It locks down the *contracts* (shared state, agent interface,
> LLM gateway, tool wrappers, folder layout) so multiple developers can build
> different agents at the same time without breaking each other.
>
> Companion docs: [`AGENTS.md`](./AGENTS.md) — responsibilities & tool primers;
> [`PHASES.md`](./PHASES.md) — end-to-end phase structure & each agent's I/O.
> This doc = **how we build it together**.
>
> All paths and interfaces below match the real code in `services/implementation/`.

---

## 0. Confirmed Agents & Tools (locked)

| # | Agent | Core Tools (keep) | Optional / Later | LLM |
|---|-------|-------------------|------------------|-----|
| 1 | Code Generation | LangGraph, LLM, GitPython, Jinja2 | — | ✅ |
| 2 | Code Review | LLM, Ruff (Python), ESLint (React/TS) | Tree-sitter (custom rules only) | ✅ |
| 3 | Refactoring | LLM, Black (Python), Prettier (React/TS) | Tree-sitter (custom rules only) | ✅ |
| 4 | Debugging | LLM, Python subprocess | Docker (add when isolation needed) | ✅ |
| 5 | Unit Test Generator | LLM, pytest (Python), Jest (React) | — | ✅ |
| 6 | Documentation | LLM | FastAPI OpenAPI (only if app is FastAPI) | ✅ |
| 7 | Security | Semgrep, pip-audit, npm audit | LLM (plain-English summaries) | ⚠️ Optional |

*All tools open source. Only paid part = LLM API.*

---

## 1. The Golden Rule for Parallel Work

**Every agent is a subclass of `BaseAgent` that implements `execute(state) -> state`:**

```python
from app.agents.base import BaseAgent
from app.graph.state import WorkflowState

class MyAgent(BaseAgent):
    name = "my_agent"

    def execute(self, state: WorkflowState) -> WorkflowState:
        ...
        return state
```

- It **reads** the state keys it needs, **writes** the keys it owns, updates
  `workflow_status`, and **returns** the state.
- It must **not** rename or delete keys owned by other agents.
- It calls the LLM only through the gateway (`self.llm.complete(...)`) — never the
  SDK directly.
- As long as everyone honors the [State Contract](#3-the-state-contract) and the
  [Agent Interface Contract](#4-agent-interface-contract), all 7 agents can be
  built, tested, and merged **independently**.

Each developer can build their agent against a **mock state** without waiting for
upstream agents to be finished.

---

## 2. Repository Layout (`services/implementation/`)

```
services/implementation/
├── docs/
│   ├── AGENTS.md            # responsibilities & tool primers
│   ├── CONTEXT.md           # THIS FILE — contracts for parallel dev
│   └── PHASES.md            # phase structure & agent I/O
├── requirements.txt
├── .env.example             # copy to .env (secrets/config)
└── app/
    ├── main.py              # FastAPI entrypoint (web layer only)
    ├── config/
    │   └── settings.py      # config from .env (API key, model, ...)  (Owner: Lead)
    ├── api/                 # WEB LAYER (input/output only, no logic)
    │   ├── routes.py
    │   ├── request_models.py
    │   └── response_models.py
    ├── graph/               # WORKFLOW ENGINE (order of agents)        (Owner: Lead)
    │   ├── state.py         # WorkflowState — the shared "clipboard"
    │   ├── graph.py         # LangGraph wiring of all 7 nodes
    │   ├── nodes.py         # one node wrapper per agent
    │   └── router.py        # conditional routing (Debugging↔Testing loop)
    ├── agents/              # THE WORKERS (one file per agent)
    │   ├── base.py          # BaseAgent (execute + _load_prompt)       (Owner: Lead)
    │   ├── code_generator.py# Agent 1   (Owner: Dev A)  ✅ built
    │   ├── code_review.py   # Agent 2   (Owner: Dev B)
    │   ├── refactoring.py   # Agent 3   (Owner: Dev C)
    │   ├── debugging.py     # Agent 4   (Owner: Dev D)
    │   ├── unit_test.py     # Agent 5   (Owner: Dev E)
    │   ├── documentation.py # Agent 6   (Owner: Dev F)
    │   └── security.py      # Agent 7   (Owner: Dev G)
    ├── services/            # SHARED SERVICES
    │   └── llm_gateway.py   # the ONE door to the LLM                  (Owner: Lead)
    ├── integrations/        # TOOL WRAPPERS (ruff, semgrep, pytest, ...)
    ├── prompts/             # <agent>.md system prompts (version-controlled)
    └── workspace/           # OUTPUT: the app the agents build
```

**Rule:** each agent lives in its **own file** under `app/agents/` and exposes one
`execute(state)` method. Nobody edits another agent's file → no merge conflicts.

---

## 3. The State Contract

The **most important part of this doc.** The shared object passed between all
agents is `WorkflowState`, defined once in
[`app/graph/state.py`](../app/graph/state.py) (`TypedDict, total=False`). Never
change a key's meaning without telling the team.

```python
from typing import TypedDict

class WorkflowState(TypedDict, total=False):
    # ---- Identity / INPUT ----
    project_id: str
    design_package: str            # design pack (text or workspace-path reference)

    # ---- Agent 1: Code Generation (OWNS) ----
    generated_code: str

    # ---- Agent 2: Code Review (OWNS) ----
    review_report: str

    # ---- Agent 3: Refactoring (OWNS) ----
    refactored_code: str

    # ---- Agent 4: Debugging (OWNS) ----  # TODO: add these to state.py
    debugged_code: str
    run_logs: str
    run_success: bool
    debug_attempts: int            # retry-loop guard

    # ---- Agent 5: Unit Test Generator (OWNS) ----
    unit_tests: str
    test_results: dict             # TODO: add to state.py — {"passed": int, "failed": int, "failures": [...]}

    # ---- Agent 6: Documentation (OWNS) ----
    documentation: str

    # ---- Agent 7: Security (OWNS) ----
    security_report: str

    # ---- Control (every agent updates) ----
    workflow_status: str
```

> **Note:** the 5 fields marked `TODO` (`debugged_code`, `run_logs`, `run_success`,
> `debug_attempts`, `test_results`) are **not yet in `state.py`** — the Lead must
> add them before Agents 4 & 5 are wired. See [`PHASES.md`](./PHASES.md) §3.

### Ownership table (who writes what)
| State key | Written by | Read by |
|-----------|-----------|---------|
| `project_id`, `design_package` | API (input) | All |
| `generated_code` | Agent 1 | 2, 3, 6, 7 |
| `review_report` | Agent 2 | 3 |
| `refactored_code` | Agent 3 | 4, 5 |
| `debugged_code`, `run_logs`, `run_success`, `debug_attempts` | Agent 4 | 5, router (loop) |
| `unit_tests`, `test_results` | Agent 5 | router (loop back on fail) |
| `documentation` | Agent 6 | (report only) |
| `security_report` | Agent 7 | (report only) |
| `workflow_status` | every agent | API, router |

> **Golden rule restated:** you may READ any key; you may only WRITE the keys your
> agent OWNS.

---

## 4. Agent Interface Contract

Every agent is one file `app/agents/<name>.py` subclassing `BaseAgent`
(see [`app/agents/base.py`](../app/agents/base.py)):

```python
# app/agents/<name>.py
from app.agents.base import BaseAgent
from app.graph.state import WorkflowState

class CodeReviewAgent(BaseAgent):
    name = "code_review"

    def execute(self, state: WorkflowState) -> WorkflowState:
        """
        Reads:  generated_code
        Writes: review_report, workflow_status
        """
        system = self._load_prompt("code_review")           # app/prompts/code_review.md
        # 1. read inputs from state
        # 2. do the work (run tool wrappers + self.llm.complete(...))
        # 3. write results into state
        state["review_report"] = ...
        state["workflow_status"] = "reviewed"
        return state
```

`BaseAgent` provides `self.llm` (the gateway) and `self._load_prompt(name)`.
Use [`code_generator.py`](../app/agents/code_generator.py) as the reference.

That's all the orchestrator needs. As long as your class matches this, the Lead
can wire it into the graph even before it's fully implemented (start with a stub
that just returns state).

---

## 5. The One LLM Door — the Gateway (build/keep central)

Every agent calls the LLM **only** through the shared gateway
[`app/services/llm_gateway.py`](../app/services/llm_gateway.py) — never the
provider SDK directly. This keeps retries, logging, and provider choice in one place.

```python
# inside an agent (BaseAgent gives you self.llm):
text = self.llm.complete(
    prompt=f"Design Package:\n\n{state.get('design_package', '')}",
    system=self._load_prompt("code_generation"),
)
```

**Contract:** `llm_gateway.complete(prompt, *, system=None, max_tokens=None) -> str`.

- Today the gateway targets **Claude (Anthropic)**; model/keys come from
  `app/config/settings.py` (`ANTHROPIC_API_KEY`, `LLM_MODEL`).
- Swapping to another provider (OpenAI, Ollama) is a change **inside the gateway
  only** — agents never import `anthropic`/`openai`/`ollama` themselves.

---

## 6. Tool Wrappers (so tools are easy to swap/mock)

Wrap each external tool in a small helper under `app/integrations/` that returns
**structured Python data**, not raw text. This lets developers mock tools while
building. Suggested wrappers:

| Wrapper (in `app/integrations/`) | Tool | Returns |
|---------|------|---------|
| `ruff_runner.run(path)` | Ruff | `list[Finding]` |
| `eslint_runner.run(path)` | ESLint | `list[Finding]` |
| `black_runner.format(path)` | Black | None (formats in place) |
| `prettier_runner.format(path)` | Prettier | None |
| `subprocess_runner.run(cmd)` | subprocess | `(stdout, stderr, exit_code)` |
| `pytest_runner.run(path)` | pytest | `test_results` dict |
| `jest_runner.run(path)` | Jest | `test_results` dict |
| `semgrep.scan(path)` | Semgrep | `list[Finding]` |
| `pip_audit.run(path)` | pip-audit | `list[dict]` |
| `npm_audit.run(path)` | npm audit | `list[dict]` |
| `templates.render(name, vars)` | Jinja2 | `str` |
| `github.commit(repo, files, msg)` | GitPython | None |

Most tools are invoked via a shared `subprocess_runner` under the hood (they have
`--json` / `--format=json` output that's easy to parse). Empty placeholder files
already exist in `app/integrations/` — fill in the ones your agent needs.

---

## 7. Orchestration (LangGraph) — Owner: Lead

Wired across `app/graph/`: nodes wrap agents ([`nodes.py`](../app/graph/nodes.py)),
edges + the loop live in [`graph.py`](../app/graph/graph.py) and
[`router.py`](../app/graph/router.py).

```python
# app/graph/nodes.py — one wrapper per agent (agents instantiated once)
from app.agents.code_review import CodeReviewAgent
_code_review = CodeReviewAgent()
def code_review_node(state): return _code_review.execute(state)

# app/graph/router.py — the one conditional edge (Testing -> Debugging loop)
MAX_DEBUG_ATTEMPTS = 3
def route_after_tests(state):
    failed = state.get("test_results", {}).get("failed", 0)
    if failed > 0 and state.get("debug_attempts", 0) < MAX_DEBUG_ATTEMPTS:
        return "debugging"     # loop back
    return "documentation"

# app/graph/graph.py — wiring
from langgraph.graph import END, START, StateGraph
from app.graph import nodes, router
from app.graph.state import WorkflowState

def build_graph():
    g = StateGraph(WorkflowState)
    g.add_node("code_generator", nodes.code_generator_node)
    g.add_node("code_review",    nodes.code_review_node)
    g.add_node("refactoring",    nodes.refactoring_node)
    g.add_node("debugging",      nodes.debugging_node)
    g.add_node("unit_test",      nodes.unit_test_node)
    g.add_node("documentation",  nodes.documentation_node)
    g.add_node("security",       nodes.security_node)

    g.add_edge(START, "code_generator")
    g.add_edge("code_generator", "code_review")
    g.add_edge("code_review", "refactoring")
    g.add_edge("refactoring", "debugging")
    g.add_edge("debugging", "unit_test")
    g.add_conditional_edges("unit_test", router.route_after_tests,
                            {"debugging": "debugging", "documentation": "documentation"})
    g.add_edge("documentation", "security")
    g.add_edge("security", END)
    return g.compile()

workflow = build_graph()
```

> Today `graph.py` only wires `code_generator -> END` and `router.py` is empty —
> this is the target wiring. See [`PHASES.md`](./PHASES.md) §4 & §8.

---

## 8. How to Develop in Parallel (the workflow)

1. **Lead builds the foundation first (Sprint 0):** confirm `state.py` (+ the 5 TODO
   fields), `llm_gateway.py`, `base.py`; add all 7 nodes in `nodes.py` and edges in
   `graph.py`/`router.py` as **stubs** (agent `execute` just returns state).
2. **Each dev takes one agent file** (see ownership in §2) and implements
   `execute(state)` against the locked State Contract.
3. **Devs test in isolation** using a mock state:
   ```python
   from app.agents.code_review import CodeReviewAgent
   mock = {"project_id": "p1", "generated_code": "def add(a,b): return a+b",
           "workflow_status": "code_generated"}
   result = CodeReviewAgent().execute(mock)
   assert "review_report" in result   # for Agent 2
   ```
4. **Merge:** because each agent is its own file + honors the contract, merges are
   conflict-free. Replace stubs one by one.
5. **Integration:** Lead runs the full graph end-to-end once ≥2 agents are real.

---

## 9. Setup — Getting Started

**Python deps** — add these to `requirements.txt` (current file is missing the
tool deps):
```
# already present: fastapi, uvicorn, langgraph, pydantic, pydantic-settings, anthropic, pytest, httpx
gitpython
jinja2
ruff
black
pip-audit
semgrep
```

**Node deps** (only for React targets):
```
eslint
prettier
jest
# npm audit is built into npm
```

**Environment (`.env`, from `.env.example`):**
```
ANTHROPIC_API_KEY=...          # required for the LLM gateway
LLM_MODEL=claude-opus-4-8      # default
LOG_LEVEL=INFO
```

**First commands (Windows):**
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

---

## 10. Definition of Done (per agent, for the POC)

An agent is "done" for the POC when:
- [ ] It subclasses `BaseAgent` and implements `execute(state) -> state`.
- [ ] It reads only the keys it needs and writes only the keys it owns (+ `workflow_status`).
- [ ] It calls the LLM only via `self.llm.complete(...)`.
- [ ] Its tool wrappers (in `app/integrations/`) return structured data (not raw text).
- [ ] It has a system prompt in `app/prompts/<name>.md`.
- [ ] It runs standalone against a mock state without errors.
- [ ] It has one happy-path test under `app/tests/`.

---

## Quick Reference — Ownership Assignment

| Agent | File | Suggested Owner | Depends on (upstream) |
|-------|------|-----------------|-----------------------|
| Foundation (state, graph, nodes, router, gateway) | `app/graph/`, `app/services/llm_gateway.py` | **Lead** | — |
| 1. Code Generation | `app/agents/code_generator.py` | Dev A | design_package |
| 2. Code Review | `app/agents/code_review.py` | Dev B | generated_code |
| 3. Refactoring | `app/agents/refactoring.py` | Dev C | generated_code, review_report |
| 4. Debugging | `app/agents/debugging.py` | Dev D | refactored_code |
| 5. Unit Test Generator | `app/agents/unit_test.py` | Dev E | refactored_code / debugged_code |
| 6. Documentation | `app/agents/documentation.py` | Dev F | generated/refactored code |
| 7. Security | `app/agents/security.py` | Dev G | generated/refactored code |

> Everyone can start the moment the **Lead confirms §3 (state.py) and §5
> (llm_gateway.py)** and stubs the nodes — everything else can be mocked.
