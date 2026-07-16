# Implementation Phase — End-to-End Structure & Agent I/O

> **What this doc is:** the complete map of the **Implementation phase** — the
> pipeline, the shared state, and the exact **input / output of every agent** —
> grounded in the *actual code* in `services/implementation/`.
>
> Companion docs: [`AGENTS.md`](./AGENTS.md) (responsibilities & tool primers),
> [`CONTEXT.md`](./CONTEXT.md) (contracts for parallel development).
>
> ⚠️ Where the code doesn't yet have a needed piece, it's marked **`TODO`** with
> exactly what to add. Don't assume everything below already exists — check the
> "Status" column.

---

## 1. Where this phase sits

```
Requirements ──▶ Design ──▶ [ IMPLEMENTATION ] ──▶ Testing
                                  ▲ this doc
```

- **Input contract:**  `contracts/design-to-implementation`  → a **Design Package**
- **Output contract:** `contracts/implementation-to-testing` → an **Implementation Package** (working code + tests + docs + security report)
- **Service:** `services/implementation` (FastAPI + LangGraph). Team 3.

---

## 2. The 4-layer architecture (how a request flows)

```
POST /implementation/start                     app/main.py  ── boots FastAPI
        │
        ▼
┌─────────────────────────────┐
│ FastAPI  (web layer)        │  app/api/routes.py + request_models/response_models
│ validates input, no logic   │
└──────────────┬──────────────┘
               │ builds initial WorkflowState, calls workflow.invoke()
               ▼
┌─────────────────────────────┐
│ LangGraph (workflow engine) │  app/graph/graph.py + nodes.py + router.py
│ runs agents in order, carries state
└──────────────┬──────────────┘
               │ runs each node
               ▼
┌─────────────────────────────┐
│ Agents (the workers)        │  app/agents/*.py  (one job each)
│ never call the SDK directly │
└──────────────┬──────────────┘
               │ self.llm.complete(...)
               ▼
┌─────────────────────────────┐
│ LLM Gateway (one door to AI)│  app/services/llm_gateway.py  (Claude only place)
└─────────────────────────────┘
```

**Request lifecycle (real, from the code):**
1. `POST /implementation/start` with `{project_id, design_package}` → `StartRequest`.
2. `routes.py` builds the initial state: `{project_id, design_package, workflow_status: "started"}`.
3. `workflow.invoke(initial)` runs the LangGraph pipeline.
4. Each node runs its agent's `execute(state)`; agents read/write state keys.
5. Response `StartResponse` returns `{project_id, workflow_status, generated_code}`.

> **Current API limit:** it's **synchronous** — one HTTP call runs the whole pipeline.
> `TODO` (already noted in `routes.py`): move to background execution + a
> `GET /status/{project_id}` endpoint as the pipeline grows.

---

## 3. The Shared State (the "clipboard" every agent reads/writes)

Defined in [`app/graph/state.py`](../app/graph/state.py) as `WorkflowState`
(`TypedDict, total=False`). This is the **single contract** for all agents.

| Field | Type | Written by | Exists in code? |
|-------|------|-----------|-----------------|
| `project_id` | str | API (input) | ✅ |
| `design_package` | str | API (input) | ✅ |
| `generated_code` | str | Agent 1 Code Generation | ✅ |
| `review_report` | str | Agent 2 Code Review | ✅ |
| `refactored_code` | str | Agent 3 Refactoring | ✅ |
| `unit_tests` | str | Agent 5 Unit Test | ✅ |
| `documentation` | str | Agent 6 Documentation | ✅ |
| `security_report` | str | Agent 7 Security | ✅ |
| `workflow_status` | str | every agent (updates it) | ✅ |
| `debugged_code` | str | Agent 4 Debugging | ❌ **TODO add** |
| `run_logs` | str | Agent 4 Debugging | ❌ **TODO add** |
| `run_success` | bool | Agent 4 Debugging | ❌ **TODO add** |
| `debug_attempts` | int | Agent 4 (loop guard) | ❌ **TODO add** |
| `test_results` | dict | Agent 5 (pass/fail) | ❌ **TODO add** |

> **Note:** fields hold either the artifact text or (recommended as the code grows)
> a **workspace path reference** — `settings.workspace_dir` = `app/workspace`.
> The 5 `TODO` fields are needed for the Debugging↔Testing loop and must be added
> to `state.py` before Agents 4 & 5 are wired.

---

## 4. The Pipeline (order + routing)

Per `DEVELOPER_GUIDE.md`, the agent order is:

```
Design Package
  → 1 Code Generation → 2 Code Review → 3 Refactoring → 4 Debugging
  → 5 Unit Test → 6 Documentation → 7 Security
→ Implementation Package
```

**The one loop:** Unit Test (5) ↔ Debugging (4) — if tests fail and attempts remain,
route back to Debugging; else continue to Documentation.

```
        ┌───────────────────────────────────┐
        │                                   │ tests failed & attempts < MAX
        ▼                                   │
... → 4 Debugging → 5 Unit Test ──▶ router ─┘
                                    │ tests passed (or attempts exhausted)
                                    ▼
                               6 Documentation → 7 Security → END
```

- Wired today in [`graph.py`](../app/graph/graph.py): **only** `code_generator → END`.
- [`router.py`](../app/graph/router.py) is **empty** — the loop's conditional edge
  lives here. **`TODO`: implement `route_after_tests`** and register it with
  `graph.add_conditional_edges(...)`.
- Each agent gets a node wrapper in [`nodes.py`](../app/graph/nodes.py) — only
  `code_generator_node` exists today; **`TODO`: add 6 more.**

---

## 5. Per-Agent Specification (Input → Process → Output)

Each agent subclasses `BaseAgent` and implements `execute(state) -> state`.
It loads its system prompt via `self._load_prompt("<name>")` from `app/prompts/`.

---

### Agent 1 — Code Generation  ✅ BUILT
| | |
|---|---|
| **File** | [`app/agents/code_generator.py`](../app/agents/code_generator.py) |
| **Reads** | `design_package` |
| **Writes** | `generated_code`, `workflow_status = "code_generated"` |
| **Prompt** | `app/prompts/code_generation.md` ✅ |
| **Tools** | LLM (via gateway), *(planned)* Jinja2 templates, GitPython |
| **Process** | Load prompt → `llm.complete(prompt=design_package, system=prompt)` → store code |
| **Status** | ✅ Implemented (reference example for all others) |

---

### Agent 2 — Code Review  ❌ STUB (0 lines)
| | |
|---|---|
| **File** | `app/agents/code_review.py` |
| **Reads** | `generated_code`, (`language`) |
| **Writes** | `review_report`, `workflow_status = "reviewed"` |
| **Prompt** | `app/prompts/code_review.md` — **TODO create** |
| **Tools** | **Ruff** (Python), **ESLint** (React/TS), LLM to explain/rank |
| **Integration wrapper** | `app/integrations/` — **TODO** `ruff_runner`, `eslint_runner` |
| **Process** | Run linters → collect structured findings → LLM explains + finds design-level issues → write `review_report` |
| **Status** | ❌ Empty — to build |

---

### Agent 3 — Refactoring  ❌ STUB (0 lines)
| | |
|---|---|
| **File** | `app/agents/refactoring.py` |
| **Reads** | `generated_code`, `review_report` |
| **Writes** | `refactored_code`, `workflow_status = "refactored"` |
| **Prompt** | `app/prompts/refactoring.md` — **TODO create** |
| **Tools** | LLM (proposes changes), **Black** (Python), **Prettier** (React/TS) |
| **Integration wrapper** | **TODO** `black_runner`, `prettier_runner` |
| **Process** | LLM restructures (behavior must not change) → format code → write `refactored_code`. Behavior verified by Agent 5 |
| **Status** | ❌ Empty — to build |

---

### Agent 4 — Debugging  ❌ STUB (0 lines)
| | |
|---|---|
| **File** | `app/agents/debugging.py` |
| **Reads** | `refactored_code` (or `generated_code`) |
| **Writes** | `debugged_code`, `run_logs`, `run_success`, `debug_attempts` **(all TODO in state.py)** |
| **Prompt** | `app/prompts/debugging.md` — **TODO create** |
| **Tools** | **Python subprocess** (run/build the app), LLM (analyze errors), *(later)* **Docker** |
| **Integration wrapper** | `app/integrations/docker.py` (empty) + a `subprocess` runner — **TODO** |
| **Process** | Run app → capture stdout/stderr + exit code → if error, LLM proposes fix → re-run → loop until clean or `debug_attempts` hits MAX |
| **Status** | ❌ Empty — to build. **Needs new state fields + router loop.** |

---

### Agent 5 — Unit Test Generator  ❌ STUB (0 lines)
| | |
|---|---|
| **File** | `app/agents/unit_test.py` |
| **Reads** | `debugged_code` / `refactored_code` |
| **Writes** | `unit_tests`, `test_results` **(TODO in state.py)**, `workflow_status = "tested"` |
| **Prompt** | `app/prompts/unit_test.md` — **TODO create** |
| **Tools** | LLM (writes tests), **pytest** (Python), **Jest** (React) |
| **Integration wrapper** | `app/integrations/pytest_runner.py` (empty) — **TODO** + `jest_runner` |
| **Process** | LLM generates tests → write test files → run pytest/Jest → collect `test_results`. On failure → router loops back to Agent 4 |
| **Status** | ❌ Empty — to build |

---

### Agent 6 — Documentation  ❌ STUB (0 lines)
| | |
|---|---|
| **File** | `app/agents/documentation.py` |
| **Reads** | final code (`debugged_code`/`refactored_code`), `design_package` |
| **Writes** | `documentation`, `workflow_status = "documented"` |
| **Prompt** | `app/prompts/documentation.md` — **TODO create** |
| **Tools** | LLM (README + docstrings), **FastAPI OpenAPI** *(only if generated app is FastAPI)* |
| **Process** | LLM writes README + inline docs → if FastAPI app, export OpenAPI spec → write `documentation` |
| **Status** | ❌ Empty — to build |

---

### Agent 7 — Security  ❌ STUB (0 lines)
| | |
|---|---|
| **File** | `app/agents/security.py` |
| **Reads** | final code + dependency files (`requirements.txt`, `package.json`) |
| **Writes** | `security_report`, `workflow_status = "completed"` |
| **Prompt** | *(optional)* `app/prompts/security.md` — LLM only summarizes |
| **Tools** | **Semgrep**, **pip-audit** (Python), **npm audit** (Node); LLM optional for plain-English summary |
| **Integration wrapper** | `app/integrations/semgrep.py` (empty) — **TODO** + `pip_audit`, `npm_audit` |
| **Process** | Semgrep static scan + dependency audit → collect findings → (optional) LLM summary → write `security_report` |
| **Status** | ❌ Empty — to build |

---

## 6. Entry & Exit Contracts

**Entry — Design Package (input):**
- Delivered as `design_package` (string / reference) in `StartRequest`.
- Per repo README: *27 inputs, 20 mandatory*. Full schema lives in
  `contracts/design-to-implementation`. Sample fixtures in
  `fixtures/ecommerce_complete/` (architecture.md, openapi.yaml, schema.sql,
  routes.json, frontend/backend-structure.json, etc.).

**Exit — Implementation Package (output):**
- Artifacts A1–A7 + `tech-stack.json`, per `contracts/implementation-to-testing`.
- In state terms: `generated_code` → `refactored_code`/`debugged_code`,
  `unit_tests`, `documentation`, `security_report`, final `workflow_status`.

---

## 7. `workflow_status` progression (state machine)

```
started
  → code_generated   (Agent 1)
  → reviewed          (Agent 2)
  → refactored        (Agent 3)
  → debugging / debug_ok   (Agent 4)   ⇄ loop with tested
  → tested            (Agent 5)
  → documented        (Agent 6)
  → completed         (Agent 7)
```
Use these exact strings so the (future) `GET /status` endpoint and the router
can reason about progress.

---

## 8. Gap Summary — what must exist before the full pipeline runs

| # | Gap | Where | Owner |
|---|-----|-------|-------|
| 1 | Add 6 agent implementations | `app/agents/*.py` | Devs B–G |
| 2 | Add state fields: `debugged_code, run_logs, run_success, debug_attempts, test_results` | `app/graph/state.py` | Lead |
| 3 | Register 6 node wrappers | `app/graph/nodes.py` | Lead |
| 4 | Wire all edges + conditional loop | `app/graph/graph.py` | Lead |
| 5 | Implement `route_after_tests` | `app/graph/router.py` (empty) | Lead |
| 6 | Build tool wrappers | `app/integrations/*` (all empty) | each agent's dev |
| 7 | Create prompt files | `app/prompts/*.md` | each agent's dev |
| 8 | Add missing deps: `ruff, black, jinja2, gitpython, pip-audit, semgrep` | `requirements.txt` | Lead |
| 9 | Extend API response for all artifacts + async `/status` | `app/api/*` | Lead |

> **Bottom line:** Agent 1 + the 4-layer skeleton + LLM gateway are real and
> working. Everything else in this doc is the roadmap — build against the state
> contract in §3 and the per-agent specs in §5.
