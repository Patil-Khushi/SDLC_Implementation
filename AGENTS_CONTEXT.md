# Building the Implementation-Phase Agents — Ground-Truth Context

> **Read this before building or modifying any agent in `services/implementation/`.**
> It is the canonical, code-verified reference. Everything here was checked against the actual
> files (not memory). Authority order: `DEVELOPER_GUIDE.md` → `CLAUDE.md` → this file. Where any
> of them conflict, the guide wins; this file adds the *current state* the guide predates.

---

## 1. The pipeline and what is actually built

```
Design Package
  -> Code Generation   writes code            [BUILT]  app/agents/code_generator.py
  -> Code Review        reviews code           [BUILT]  app/agents/code_review.py
  -> Refactoring        fixes issues found     [BUILT]  app/agents/refactoring.py
  -> Debugging          fixes bugs             [BUILT]  app/agents/debugging.py
  -> Unit Test          writes tests           [BUILT]  app/agents/unit_test.py
  -> Documentation      writes docs            [BUILT]  app/agents/documentation.py
  -> Security           security scan          [BUILT]  app/agents/security.py
-> Implementation Package
```

Also built: `app/agents/base.py` (BaseAgent), `app/agents/repair.py` (the code-gen repair loop
— NOT a pipeline stage, see §7).

> **Correction (superseding the "five stubs, 0 bytes" claim this section used to make):** all
> five agents above are now built — verified directly by file size (`refactoring.py` 29,946B,
> `debugging.py` 9,022B, `unit_test.py` 8,434B, `documentation.py` 4,373B, `security.py` 13,939B —
> none are 0 bytes). `unit_test.py` was read in full this session: it's a close twin of
> `code_generator.py` (per-work-item prompt → JSON parse-with-one-retry → `executor.write_file`),
> writes only `unit_tests` (a `list[str]` of paths, not `str` — §4's table below is also wrong on
> this), never calls git, and does not use `pytest_runner.py` (still genuinely a 0-byte stub, see
> §6) at all — test *execution* is the separate fixed `unit_test_run` node in `graph.py`, not this
> agent. The other four agents' §9 write-ups below were NOT re-verified this pass beyond file
> size — treat their job/reads/writes/open-decision details as unconfirmed until someone reads
> the actual files, the same way this note just did for Unit Test.

---

## 2. The four layers (never mix them)

```
FastAPI (app/api/)      HTTP in/out only, no agent logic
   │
LangGraph (app/graph/)  runs agents in order, carries WorkflowState, does routing
   │
Agents (app/agents/)    one agent = one job; NEVER import the model SDK
   │
LLM Gateway (app/services/llm_gateway.py)   the ONLY place that imports `anthropic`
```

The nine rules (DEVELOPER_GUIDE.md §9), condensed: 1 agent = 1 job · agents call the model only
via `self.llm` · agents write only the state fields they own · workflow order lives in
`graph.py`, not agents · no agent logic in `api/` · outside tools live in `integrations/` ·
prompts live in `app/prompts/*.md` · write a test per agent · never commit `.env`.

---

## 3. The agent contract (copy this shape exactly)

Every agent subclasses `BaseAgent` (`app/agents/base.py`) and implements ONE method:

```python
from app.agents.base import BaseAgent
from app.graph.state import WorkflowState

class XxxAgent(BaseAgent):
    name = "xxx"                                   # used in logs/trace

    def execute(self, state: WorkflowState) -> WorkflowState:
        report = self.llm.complete(                # ONLY model access
            prompt="...",
            system=self._load_prompt("xxx"),       # loads app/prompts/xxx.md
        )
        state["<the_one_field_i_own>"] = report    # write ONLY your field
        state["workflow_status"] = "xxx_done"
        return state
```

`BaseAgent` gives you exactly two things: `self.llm` (the gateway) and
`self._load_prompt(name)` (reads `app/prompts/<name>.md`). Nothing else.

**DI convention (follow it, tests depend on it):** accept optional `llm` + any integration in
`__init__` and fall back to the module singleton / provider at call time. See
`CodeGeneratorAgent.__init__` and `CodeReviewAgent.__init__`.

---

## 4. WorkflowState — the shared clipboard (VERIFIED from `app/graph/state.py`)

`WorkflowState` is a `TypedDict(total=False)`. Full field list and **who may write each**:

| Field | Type | Owner (writes it) | Notes |
|---|---|---|---|
| `project_id` | str | run setup | echoed everywhere |
| `run_id` | str | run setup | this service's id |
| `attempt` | int | orchestrator | echoed unchanged — **never increment here** |
| `design_package` | dict | input | artifact bundle (SKILL.md, openapi.yaml, schema.sql, …) |
| `repo_url` | str | input / Code Gen | public GitHub URL; **Code Review clones this** |
| `work_items` | list[WorkItem] | Code Gen internals | decomposed plan |
| `work_item_index` | int | graph (`select`) | cursor |
| `current_work_item` | WorkItem\|None | graph (`select`) | item being generated |
| `generated_code` | list[str] | Code Gen | workspace-relative file paths |
| `codegen_ok` | bool | Code Gen | routing signal |
| `gate_result` | GateResult\|None | gate node | routing source |
| `repair_attempt` | int | repair node | LOCAL counter, reset per item, ≠ `attempt` |
| `generation_summary` | str | Code Gen | free text |
| `generation_metrics` | dict | Code Gen | metrics |
| `review_report` | str | **Code Review** | Markdown report content |
| `review_report_path` | str | **Code Review** | path under `reports/` |
| `refactored_code` | str | **Refactoring** | built — shape not re-verified this pass |
| `unit_tests` | list[str] | **Unit Test** | built — paths of test files written (verified) |
| `documentation` | str | **Documentation** | built — shape not re-verified this pass |
| `security_report` | str | **Security** | built — shape not re-verified this pass |
| `workflow_status` | str | any (lifecycle) | current stage |

**Golden rule:** an agent reads what it needs and writes ONLY its own field.
`new_state(...)` initializes the identity/input/code-gen fields; downstream output fields are
left unset until their agent runs.

> There is currently **no state field for Debugging output** — see the open decision in §9.

---

## 5. The LLM gateway (`app/services/llm_gateway.py`)

Two methods, both return the model's text:

- `self.llm.complete(prompt, system=None, max_tokens=None) -> str` — one-shot. Use for review,
  docs, test generation, etc.
- `self.llm.complete_with_tools(prompt, system=None, tools=[...], max_iters=4) -> str` — runs a
  tool-use loop; the model decides when to call the tools you pass. Use when the agent must let
  the model inspect/act (see `repair.py`). Tool binding + the SDK live inside the gateway, so the
  agent still imports no SDK.

Test double: `FakeLLMGateway(responses=[...] | callable, default=...)` — same surface, no
network. Inject it via the agent's `llm=` param.

Model config comes from settings (`llm_model`, default `claude-opus-4-8`; `llm_max_tokens`;
`llm_thinking`). Never hard-code a model in an agent.

---

## 6. Integrations — what exists vs. what is a stub (`app/integrations/`)

| File | Status | What it gives you |
|---|---|---|
| `executor.py` | **BUILT** | The exec-sandbox chokepoint. `Executor` ABC + `FakeExecutor` (tests) + `MCPExecutor` (real). Methods: `run_command`, `write_file`, `read_file`, `git_status/diff`, `install_package`, fixed checks `compile/build/test/lint`, `git_commit`, `get_repair_tools`. `get_executor()/set_executor()` provider. |
| `review_sandbox.py` | **BUILT** | Ephemeral per-review sandbox. `ReviewSandbox` ABC + `FakeReviewSandbox` (tests) + `DockerReviewSandbox` (real: `docker run/exec/rm`). Methods: `clone`, `run`, `read_text`, `list_files`, `close` (context manager). `get_review_sandbox()` provider. |
| `sonarqube.py` | **BUILT** | `SonarQubeClient.fetch_issues()` reads issues from a SonarQube server over HTTP. `SonarResult`/`SonarIssue`. `get_sonarqube_client()`. Injectable `http_get` for tests. |
| `pytest_runner.py` | **STUB (0 bytes)** | intended: run pytest → results. Unit Test agent will need this. |
| `github.py` | **STUB (0 bytes)** | intended: GitHub API (push/clone/PR). Nothing exists. |
| `docker.py` | **STUB (0 bytes)** | intended: container mgmt. |
| `figma.py` | **STUB (0 bytes)** | intended: design assets. |

Reuse `RunResult`/`CheckResult` dataclasses from `executor.py` for tool outcomes rather than
inventing new ones.

---

## 6b. Git branching model (DECIDED)

All phases operate on **one working branch** (default **`dev`**, `settings.working_branch`); `main`
is the protected release branch. There is **no per-branch review** — every phase acts on `dev`.

```
main  (protected)
 └── dev  (working branch — every phase acts here)
       Code Generation → pushes generated code to  dev,  sets state.repo_url + state.branch
       Code Review     → clones  dev  (read-only), pins state.commit_sha, writes the report
       Refactoring     → clones  dev  (read-write), commits fixes back to  dev
       Debugging       → fixes on  dev
       Unit Test       → generates tests on  dev
       Documentation   → docs on  dev
       Security        → security scan on  dev
       ── after Security PASSES ──▶  MERGE  dev → main   (final step, via the human_review gate)
```

The merge to `main` is the **last step of the whole phase, performed only after the Security scan
passes** — ideally as a dedicated finalize/merge step gated on the existing `human_review`
approval (keeps "one agent = one job" — the Security agent scans, it does not merge).

State fields: `repo_url` (public GitHub URL), `branch` (working branch), `commit_sha` (exact
reviewed/refactored commit, pinned by Code Review for reproducibility).

Status today: Code Review clones `state.branch` (falls back to `working_branch`) and records the
HEAD SHA. Code Generation's push, Refactoring's/Debugging's commits, and the final post-Security
`dev → main` merge are **not built yet** — they need `github.py` (empty stub). Build to this model.

---

## 7. TWO execution models — this is the most important section

There are **two different sandboxes**, and choosing the wrong one is the #1 source of confusion:

### A. exec-sandbox (`executor.py`) — used by Code Generation + Repair
- A long-lived MCP sandbox container. Files are **written into its workspace** and live there.
- Code Generation writes files with `executor.write_file`; the **gate** runs `compile`+`build`;
  **commit** does a local `git_commit`. The **repair** agent (tool loop) fixes gate failures.
- This is the "hybrid two-path" doctrine in `CLAUDE.md` (fixed path = your node code; repair
  path = LLM + `get_repair_tools()`).

### B. review sandbox (`review_sandbox.py`) — used by Code Review
- An **ephemeral** container created per review. It **clones `repo_url` from GitHub**, runs
  **static** analysis (ruff/eslint + sonar-scanner), is **torn down**, and the agent writes a
  Markdown report to `reports/`.
- Code Review **never executes** the project and **never modifies** code (Testing runs it;
  Refactoring changes it).

### KNOWN GAP (do not hallucinate a connection): 
Code Generation currently writes files into the **exec-sandbox workspace** and commits locally —
it does **NOT** push to GitHub or set `repo_url`. Code Review **clones `repo_url`**. **Nothing
currently sets `repo_url` from Code Generation.** So today the two stages are not wired end to
end. Before the pipeline runs whole, someone must decide: does Code Generation push the repo to
GitHub and set `repo_url` (needs `github.py`), or does Code Review read from the sandbox
workspace instead? **This is unresolved — state it, don't invent it.**

---

## 8. Wiring an agent into the graph (`app/graph/`)

Three files, verified current shape:

**`nodes.py`** — one thin function per agent; agents instantiated once at import:
```python
_xxx = XxxAgent()
def xxx_node(state: WorkflowState) -> WorkflowState:
    return _xxx.execute(state)
```

**`router.py`** — conditional edges read state and return the next node name. `REPAIR_CAP = 3`
lives here (local repair cap, separate from orchestrator `attempt`).

**`graph.py`** — the diagram below predates the debugging/unit_test/refactoring/documentation/
security/finalize/package agents being built and is now historical/superseded — **do not treat it
as current**. Read `app/graph/graph.py`'s own module docstring for the real, up-to-date graph
shape (it is code-adjacent and gets kept in sync there; duplicating the full diagram here would
just create a second copy that drifts, which is exactly what happened to this one):
```
START -> select
select        -> code_generator      (item pending)   | code_review (plan exhausted)
code_generator-> gate | escalate
gate          -> commit | repair | escalate
commit        -> select
repair        -> gate
escalate      -> human_review -> END
code_review   -> END
```
There is no `human_review` node and no interrupt anymore (HITL was removed — a completed plan
auto-commits; `escalate` sets `needs_human_review` and ends the run, see `CLAUDE.md`). The graph
compiles with a disk-backed **`SqliteSaver`** checkpointer (`app/graph/graph.py::_build_checkpointer`),
not `MemorySaver` — so a run that crashes mid-graph can resume via `workflow.invoke(None, config)`
instead of restarting; it is kept for that, and for `get_state()` reading a finished run, not for
any interrupt.

To add the next stage, insert your node between `code_review` and `END` (and re-point the tail).
Follow DEVELOPER_GUIDE §6's 4-step recipe: prompt → agent → node → edge.

---

## 9. Per-agent build specs for the five stubs

For each: its job (from the guide), what it reads, the field it writes, the execution model, and
the **open decisions** (mark these; do not guess).

### Refactoring (`refactoring.py`, prompt `refactoring.md`)
> ⚠ File is now BUILT (29,946 bytes, confirmed by size only — not re-read this session). The
> job/reads/writes/open-decision details below predate that and may be stale; re-verify against
> the actual file before relying on them, the way §1's correction note did for Unit Test.
- **Job:** apply the fixes the review names. It **changes code** (read-write).
- **Reads:** `review_report`, the code (via `repo_url` or the sandbox).
- **Writes:** `refactored_code`.
- **Execution model:** read-write — needs to clone/modify/persist. **OPEN:** where do changes
  land? (push to GitHub via unbuilt `github.py`? new branch? write back to the exec-sandbox?)
- Pattern to mirror: `repair.py` (LLM proposes file content → written back), but driven by the
  review report rather than a gate failure.

### Debugging (`debugging.py`, prompt `debugging.md`)
> ⚠ File is now BUILT (9,022 bytes) and was actively edited this session (`DEBUG_MAX_ITERS`,
> `_current_failure()`/`_build_prompt()` reading `(check_name, stderr, stdout)` from `GateCheck`,
> etc.) — the "OPEN: no debugged_code field" note below is very likely resolved/moot and should
> not be trusted; re-read the current file instead of this write-up.
- **Job:** fix bugs (logic/runtime), distinct from `repair.py` which only fixes compile/build
  gate failures inside code-gen.
- **Reads:** failing signals (tests, review). **Writes:** — **OPEN: there is no `debugged_code`
  state field.** Decide: reuse `refactored_code`, add a new field, or fold Debugging into
  Refactoring. Add the field to `state.py` + `test_state.py::ALL_FIELDS` if you add one.

### Unit Test (`unit_test.py`, prompt `unit_test.md`) — BUILT, re-verified this session
- **Job:** write test files for the already-generated, already-committed source of every work
  item, once, after Debugging's compile/build check has passed. Mirrors `code_generator.py`'s
  shape: per-work-item prompt → `{"files":[...]}` JSON parsed with one retry on failure →
  `executor.write_file`. Partial failure (one item's JSON doesn't parse) does not abort the run.
- **Reads:** each work item's own `target_files` (via `executor.read_file`) for context — nothing
  else; it never sees the project's `package.json`/`jest.config.js`, which is why its prompt
  (`unit_test.md`) must name the test framework explicitly rather than assume the model will
  infer it correctly (see the fixed Vitest/Jest mismatch bug, this session).
- **Writes:** `unit_tests` (list[str] of paths), `tests_ok` (bool — false only if work items
  existed but zero test files were written), `generation_summary`, `generation_metrics.tests_written`.
- **Does NOT commit.** Per its own docstring: "no gate/compile logic, no git, no routing." Files
  sit as uncommitted working-tree content until the separate fixed `unit_test_run` node (wired in
  `graph.py`, not this file) runs them and — only on a full pass — routes to `debug_publish`,
  which is what actually commits. A run stuck failing `unit_test_run` (e.g. a framework mismatch
  across every generated test file) means the generated tests stay uncommitted indefinitely; this
  is expected behavior, not a bug in this agent.
- **Does not use `pytest_runner.py`** (still a genuine 0-byte stub, §6) — nothing in the current
  code path calls it.

### Documentation (`documentation.py`, prompt `documentation.md`)
> ⚠ File is now BUILT (4,373 bytes, confirmed by size only). Details below not re-verified.
- **Job:** write docs (README/API docs) from the code + design package. **Reads:** code,
  `design_package`. **Writes:** `documentation`. Pure LLM; no sandbox needed unless it inspects
  the repo (then reuse `review_sandbox` read-only, like Code Review).

### Security (`security.py`, prompt `security.md`)
> ⚠ File is now BUILT (13,939 bytes, confirmed by size only). Details below not re-verified.
- **Job:** security SAST — **this is the home for Semgrep** (decided earlier: Semgrep belongs in
  Security, not Code Review). **Reads:** code. **Writes:** `security_report`.
- **Execution model:** static, like Code Review — clone via `review_sandbox`, run the scanner,
  tear down. Build a `semgrep.py` integration (ABC + Fake + real) — do not shell out from the
  agent.

---

## 10. Testing pattern (mirror `app/tests/`)

- Fakes, never network: `FakeLLMGateway`, `FakeExecutor`, `FakeReviewSandbox`.
- `conftest.py` provides `fake_gateway` (records/replays real responses under
  `tests/fixtures/llm-responses/` when `RECORD=1`), the real design-pack fixtures, and an
  autouse fixture that points `reports_dir` at a tmp dir (so tests never write into the repo).
- Every agent gets a `test_<agent>.py` asserting it writes ONLY its own field and leaves
  `run_id`/`attempt`/others untouched. Copy `test_code_review.py` (agent-level) or
  `test_graph.py` (whole-graph, stubbed LLM) as templates.
- Run: `.venv\Scripts\python.exe -m pytest app/tests -q -m "not integration"`. `integration`
  tests need the live exec-sandbox and are skipped by default.

---

## 11. Contracts (the handoff schemas) — mostly placeholders

- `contracts/design-to-implementation/` — **placeholder README only.** Stated: 27 inputs, 20
  mandatory. The actual field schema is undefined — `design_package` is a loose dict today.
- `contracts/implementation-to-testing/` — has JSON schemas for `work-item`,
  `generation-metrics`, `generation-summary`; the "A1–A7 + tech-stack.json" output is otherwise
  undefined.
- Do **not** invent contract fields. If an agent needs a design-package artifact, read it
  defensively by name (see `code_generator._artifact` / `code_review._artifact_text`).

---

## 12. File map (where everything lives)

```
app/agents/        one file per agent (base, code_generator, code_review, repair + 5 stubs)
app/graph/         state.py (clipboard) · nodes.py · router.py · graph.py
app/services/      llm_gateway.py (the model door) · artifact_service.py
app/integrations/  executor.py · review_sandbox.py · sonarqube.py (+ 4 stubs)
app/prompts/       <agent>.md system prompts (code_generation, code_review, repair exist)
app/config/        settings.py (.env-driven)
app/api/           routes.py · request_models.py · response_models.py
app/tests/         test_<thing>.py + conftest.py (fakes, fixtures)
tools/review-sandbox/Dockerfile   image for the review sandbox (git+ruff+eslint+sonar-scanner)
reports/           generated Markdown review reports (git-ignored)
docker-compose.yml service-scoped SonarQube + its Postgres
```

---

## 13. Rules of thumb to avoid hallucination

1. **Check file size before assuming behavior** — the 5 stubs and 4 integration stubs are empty.
2. **Two sandboxes, two jobs** (§7). Code-gen/repair = exec-sandbox (read-write, executes).
   Review/security = review-sandbox (ephemeral, static, read-only).
3. **Every agent owns exactly one state field** (§4). If you need a new one, add it to `state.py`
   AND `test_state.py::ALL_FIELDS`.
4. **Never import `anthropic` in an agent** — only `self.llm`.
5. **Never shell out from an agent** — go through an `integrations/` wrapper.
6. **`repo_url` handoff is unresolved** (§7). Don't assume code-gen produces it.
7. **Contracts are placeholders** (§11). Read design-package artifacts defensively by name.
