# service: implementation (Team 3)

Turns a **design pack** into working, reviewed, tested source code and publishes it to a GitHub
repo — driven by a multi-agent **LangGraph** pipeline.

**Input:** a design pack (27 inputs, 20 mandatory) — `contracts/design-to-implementation`.
**Output:** a generated product repo (scaffold on `main`, features on `dev`) + a Code Review
report — `contracts/implementation-to-testing`.

---

## Architecture

Four layers: **FastAPI → LangGraph → Agents → LLM Gateway**. Every step reads from and writes to
one shared object, `WorkflowState` (the "clipboard", defined in `app/graph/state.py`), and the
agents run automatically in sequence.

```
scaffold ─(push main early)─→ select ─┬─ code_generator → gate ─┬─ pass → feature_publish → select (loop)
                                       │                         ├─ fail & repair<3 → repair → gate
                                       │                         └─ fail & repair≥3 → escalate → END
                                       └─ plan done → commit ──→ code_review → refactoring → refactoring_publish → debug_check
                                                                                                                        │
                                          debug_check ─┬─ pass → unit_test_generate → unit_test_run → END ("completed")
                                                       ├─ fail & debug<3 → debugging → debug_check
                                                       └─ fail & debug≥3 → escalate → END
```

### The agents

| # | Agent | Does | Writes to shared state |
|---|-------|------|------------------------|
| 0 | **Scaffold** (no LLM) | renders boilerplate; in publish mode creates the repo + pushes `main` early | `generated_code`, `repo_url` |
| 1 | **Code Generator** | generates source files per work item (real Claude); `gate` checks files exist, `repair` fixes gaps (≤3) | `generated_code` |
| 2 | **Code Reviewer** | clones the pushed repo in a Docker sandbox, runs ruff + eslint + sonar-scanner, LLM writes the report | `review_report_path`, `review_findings_path` |
| 3 | **Refactoring** | agentic edit loop — reads/edits the flagged files directly to apply the review's findings; writes a report | `refactored_code`, `refactored_files`, `refactoring_report(_path)` |
| — | **Refactoring Publish** (no LLM) | FIXED: commits the edited files and pushes `dev` (no-op if nothing was edited) | `generation_summary` |
| 4 | **Debugging** | compile/build check; LLM fixes failures and re-checks (≤3) | `debug_result`, `debug_attempt` |
| 5 | **Unit Testing** | generates + runs unit tests; a pass ends the run | `unit_tests`, `test_result`, `workflow_status` |

Each feature is committed + pushed to `dev` **as it is generated** (live incremental publish), so
the GitHub repo fills in feature-by-feature during the run.

---

## Quick start

From `services/implementation/` (this folder), with the venv active:

```powershell
# The whole real flow in ONE command: generate -> publish (public repo, live) -> review ->
# refactor -> debug -> unit test. Prints the plan and waits for your approval first.
./.venv/Scripts/python.exe scripts/run_fixture.py ../fixtures/authentication --only login --project my-demo
```

You'll be asked to approve the build plan (`Proceed? [y/N]`). Type `y` to run. The repo appears on
GitHub right after the scaffold, then fills in per feature; the Code Review report lands in
`reports/<project>-<run>/report.md`; the terminal ends with a full `WorkflowState` dump.

### Run modes & flags (`scripts/run_fixture.py`)

**Real + publish + public is the default.** Opt out as needed:

| Flag | Effect |
|------|--------|
| *(none)* | real Claude, create a **public** GitHub repo, push live, run all agents |
| `-y` / `--yes` | skip the plan-approval prompt (for automation/CI) |
| `--only <substr>` | build only work items whose id contains `<substr>` (e.g. `login`) |
| `--project <name>` | repo name + local folder (owner = `$GITHUB_OWNER`); use a fresh name each run |
| `--no-publish` | build + commit locally, no GitHub repo (inline review then no-ops) |
| `--private` | private repo (⚠ inline review can't clone it, so it no-ops) |
| `--dry-run` | FakeExecutor + canned LLM — no Docker/API key/push (wiring test) |
| `--sandbox` | run inside the MCP exec-sandbox instead of the local-disk build |

`scripts/run_pipeline.py` is a sibling runner that streams each agent's stage + the shared-state
fields it wrote, live.

---

## Prerequisites

1. **Python 3.12+** and the venv: `python -m venv .venv` then `./.venv/Scripts/python.exe -m pip install -r requirements.txt`
2. **`.env`** (copy from `.env.example`): `ANTHROPIC_FOUNDRY_API_KEY`, `ANTHROPIC_FOUNDRY_BASE_URL`, `LLM_MODEL`; `GITHUB_PAT` + `GITHUB_OWNER` for publishing; `SONARQUBE_*` (optional, for Sonar findings).
3. **Authenticated `gh` CLI** (`gh auth status`) — used to create + push the repo.
4. **Docker / Rancher Desktop running** (dockerd/moby engine) — the Code Review sandbox runs in a container. Two one-time setup steps:
   ```powershell
   # build the review sandbox image (git + ruff + eslint + sonar-scanner)
   docker build -t sdlc-review-sandbox:latest tools/review-sandbox

   # start SonarQube (Community) + its Postgres
   docker compose up -d
   ```
   On WSL2 (incl. Rancher), SonarQube's Elasticsearch needs a raised map count, else the
   `impl-sonarqube` container exits on boot:
   ```powershell
   wsl -d rancher-desktop -- sysctl -w vm.max_map_count=262144
   ```
   (Not persistent — re-apply after a VM/PC restart, or set it permanently.)

The inline Code Review needs Docker up **and** a **public** repo (`--public` is the default). With
`--no-publish`, `--private`, or Docker down, the review reports "could not analyze" instead of a
real report.

---

## API

`app/main.py` exposes FastAPI: `GET /health` and `POST /implementation/start`. Note the HTTP route
takes only a `design_package` and does **not** build `work_items` yet — the scripts do that step
via `app/services/plan_builder.build_plan()`, which is why the runners drive the graph directly.

---

## Project layout

```
app/
  graph/        state.py (WorkflowState) · graph.py (wiring) · nodes.py · router.py
  agents/       code_generator · code_review · refactoring · debugging · unit_test · repair
  services/     llm_gateway · plan_builder · finding_aggregator · ...
  integrations/ executor.py (fixed/repair tools) · review_sandbox.py (Docker) · sonarqube.py
  tests/        pytest suites
scripts/        run_fixture.py · run_pipeline.py · local_executor.py · demo_server.py
tools/          review-sandbox/ (Dockerfile + eslint config for the Code Review container)
reports/        generated review reports (<project>-<run>/report.md + findings.json)
```

Authoritative conventions: **`DEVELOPER_GUIDE.md`**; Code-Generation-slice rules: **`CLAUDE.md`**;
per-agent ground truth: **`AGENTS_CONTEXT.md`**.

---

## Tests

```powershell
./.venv/Scripts/python.exe -m pytest app/tests -q
```

The graph / workflow / agent suites pass. A set of design-pack / contract-schema tests are known
pre-existing failures (missing fixtures), unrelated to the pipeline.

---

## Notes

- **No human-in-the-loop inside the graph** — a completed plan auto-commits; the only approval is
  the CLI plan gate. A repair/debug-cap failure ends the run flagged `needs_human_review`.
- **Refactoring edits are published** — after Refactoring edits the flagged files, a fixed
  `refactoring_publish` step commits exactly those files and pushes them to `dev` (a no-op when
  nothing was edited; a push failure is logged and never crashes the run), so the remote repo
  holds the reviewed + refactored code before the debug/test loop runs. It also writes a Markdown
  refactoring report next to the Code Review report (`reports/<project>-<run>/refactoring-report.md`).
