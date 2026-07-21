# services/implementation — CLAUDE.md

## Authority: DEVELOPER_GUIDE.md first

**`DEVELOPER_GUIDE.md` is the authoritative source for all conventions** in this service —
the four layers (FastAPI → LangGraph → Agents → LLM Gateway), the file layout (§4), the
`BaseAgent.execute(state) -> state` contract (§5B), the `WorkflowState` shared clipboard (§5A),
the single LLM door `self.llm` (§5C), the "add an agent" recipe (§6), and the nine rules (§9).
Read it first; follow it for everything.

**To build the remaining pipeline agents** (refactoring, debugging, unit_test, documentation,
security) read **`AGENTS_CONTEXT.md`** — a code-verified, ground-truth reference (state fields,
integrations, the two execution models, per-agent specs, and the open decisions). Read it before
writing any new agent to avoid hallucinating behavior for the empty stubs.

**This file (CLAUDE.md) adds only the deep rules for the Code Generation slice (IMP-001)** —
the compile-verify-repair machinery the guide doesn't spell out — expressed in the guide's
vocabulary (`BaseAgent.execute`, `WorkflowState`, `self.llm`, `app/integrations/executor.py`,
`app/graph/router.py`, per-agent node functions in `app/graph/nodes.py`). Where anything here
would contradict the guide, **the guide wins**.

Everything below lives *inside* the Code Generation path and its supporting layers, and stays
consistent with the guide's "one agent = one job" — the job being: turn a Design Package into
working source code, file by file, then auto-commit (no human-in-the-loop).

---

## Standing rules

### 1. Hybrid: two paths, one sandbox

Code generation runs two paths that share **the same tools on the same MCP server in the same
sandbox container** (reached only through `app/integrations/executor.py`):

- **Fixed path** — the executor's deterministic checks (`files_complete` / `compile` / `build` /
  `test` / `lint`) and `git commit`. **Your node code forms the call and invokes the executor
  directly** (deterministic `await tool.ainvoke(...)`). These are the quality gates and audit
  checkpoints — never left to the model. NOTE: the **code-generation gate runs only
  `files_complete`** (see Control flow) — a completeness check, no compile/build. `compile`/
  `build`/`test`/`lint` are exposed by the executor but owned by the downstream Unit-Test /
  Review / Security agents (generated source is committed on completeness + human approval).
- **Repair path** — inspect files / install packages / `git diff` / `git status` / reason.
  Tools are bound to the model **through `self.llm`** (the gateway's tool-calling method); the
  **model decides when** to call them. Situational recovery only.

| Concern | Fixed path | Repair path |
|---|---|---|
| Who forms the call | your node code (deterministic) | the LLM, via `self.llm`-bound tools |
| When it runs | always, in order | only after a gate failure |
| Tools used | `compile/build/test/lint`, `git_commit` | `install_package`, scoped `run_command`, `read_file`, `git_status`, `git_diff` |

### 2. git commit is fixed; the LLM only inspects

`git commit` and branch ops are **fixed steps only**. The LLM may **inspect** git (`git_diff`,
`git_status`) on the repair path, but **never commits** — `executor.get_repair_tools()`
deliberately excludes `git_commit`. Commits are the reviewer's audit trail.

### 3. The fixed gate is the router

The gate node (fixed) writes `gate_result` and is the routing source; the routing logic lives
in `app/graph/router.py`. Gate **FAIL → repair node**; **repair → back to the gate**. The local
repair cap is **~3**, held in `WorkflowState["repair_attempt"]`, and is **SEPARATE from the
orchestrator's `attempt`**, which this service **echoes and never increments**. `repair_attempt`
resets to 0 at the start of each new work item.

> **Note for future agents** (code_review, refactoring, …): the orchestrator's `attempt` is
> never incremented by this service — only the orchestrator owns it. Any local retry/repair loop
> you add must track its own counter (like `repair_attempt`) and reset it per unit of work.

### 4. Nodes never call the LLM directly

All model calls go through `self.llm` / `app/services/llm_gateway.py` (guide rule 2) — including
the repair path's tool binding. No node imports the Anthropic SDK.

### 5. External execution only through `executor.py`

Compiling, building, testing, linting, installing, and git all go through the single chokepoint
`app/integrations/executor.py` (guide rule 6: outside tools live in `integrations/`). **Nothing
else imports the MCP client or shells out.**

### 6. The sandbox is the boundary

`install_package` is **workspace-scoped** (venv / local `node_modules`) — **never global**. The
sandbox container has **no network egress except the package registries (PyPI + npm)**.

### 7. POC stack

**FastAPI** backend, **React + TypeScript** frontend. Sandbox: **Linux + bash**.

---

## Control flow

**No human-in-the-loop.** The batch-review approval interrupt (and its rework loop) was removed:
a completed plan **auto-commits**. The escalation path still flags `needs_human_review`, but no
longer pauses on an `interrupt()` — it ends the run (that pause had no resume contract and always
ended the run anyway). The graph contains **no `interrupt()`**; the checkpointer is kept only so
`get_state(config)` can read a finished run.

```
scaffold (Jinja2, once, no LLM) → select ─┬─ fresh item ──→ code_generator ─┐
                                            └─ nothing left ─→ commit → done  gate(files_complete ONLY)
                                                                                    │
    gate pass ───────────────────────────→ select (next item / done — auto-commit when exhausted)
    gate fail & repair_attempt < 3 ──────→ repair (LLM + tools) → back to gate
    gate fail & repair_attempt >= 3 ─────→ escalate: workflow_status = "needs_human_review" → done

(reset repair_attempt to 0 on each new work item; ONE run-level commit after the plan is
 exhausted; never touch the orchestrator's attempt)
```

`escalate_node` sets `needs_human_review` and ends the run — there is no separate
`human_review_node` / interrupt anymore, and no `batch_review` nodes.

**The gate is completeness-only — it does NOT compile or build.** `files_complete` (an
`Executor` check) verifies every one of the current work item's `target_files` exists on disk —
authoritative from disk, not self-reported by code_generator, so it's correct whether the files
came from code_generator or a repair pass. A missing-files failure flows through the
repair/escalate machinery. `compile`/`build`/`test`/`lint` remain on the `Executor` (for later
pipeline agents that own them) but are deliberately NOT part of this gate: generated source is
committed on completeness alone (auto), not on a green compiler and not on human approval.

---

## Missing / Ambiguous in the contracts

The contract folders currently hold **placeholder READMEs only — no schemas**. Known facts vs.
gaps:

**Input — `contracts/design-to-implementation/` (Team 2 → Team 3):**
1. Stated: the Design Package has **27 inputs, 20 mandatory**. Undefined: what those 27 fields
   are, which 20 are mandatory, their types, and how files/endpoints/tables/screens are
   enumerated for decomposition into `work_items`.
2. Undefined: how the tech stack is declared (fixed platform-wide per rule 7, or per-project),
   and how dependencies/package manifests are expressed for `install_package`.
3. Undefined: how design assets (style-guide `SKILL.md`, `tokens.json`, `mockup.html`,
   `validation-rules.json` referenced by the build plan) are carried in the package.

**Output — `contracts/implementation-to-testing/` (Team 3 → Team 4):**
4. Stated: the output is **A1–A7 + `tech-stack.json`**. Undefined: what each of A1–A7 is, their
   schemas, and which artifact carries the generated code vs. `generation_summary` /
   `generation-metrics.json`.
5. Undefined: what the Testing service actually receives (repo path, git ref/commit SHA,
   archive, or inline content), whether per-file compile/repair history is included, and what
   "done" means when a work item hit the repair cap (`needs_human_review`).

**Shared — `contracts/shared/`:**
6. Stated: there is an **`AgentResponse` envelope, run metadata, and ID rules** reviewed by all
   teams. Undefined: the envelope's fields, status enum values, error shape, and the ID/run
   metadata format — needed to know how `run_id`, `attempt`, and `workflow_status` are surfaced,
   and how the local `repair_attempt` vs. orchestrator `attempt` distinction is reported.

**Cross-cutting / not yet defined anywhere:**
7. Human-review (HITL) mechanism: **removed** — there is no batch-review approval and no
   `/implementation/{run_id}/review` endpoint; a completed plan auto-commits. The
   `needs_human_review` status is still emitted on a repair-cap failure (the run ends), but how
   the orchestrator consumes/acts on it remains undefined.
8. Orchestrator relationship: how it retries this service (a new `attempt`), how it learns a
   work item escalated, and what triggers a fresh attempt.
9. The repair cap "~3" is approximate; exact value and per-item (as modeled) vs. per-run scope
   are not contractually fixed.
