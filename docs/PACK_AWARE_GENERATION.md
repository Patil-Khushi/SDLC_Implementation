# Pack-aware generation: what was done, phase by phase

Branch: `feature/pack-aware-generation` (not pushed). Service: `SDLC_Implementation`.

## 1. Why this work was done

Two problems were reported: the debugging agent failed repeatedly and produced nothing, and the generated product (Fleet, `generated-apps/feet`) did not run. Checking the Fleet pack (`uploads/v1`) showed the pipeline was at fault, not the pack:

| Problem | Cause |
| --- | --- |
| Backend items got ~1 KB of context | Artifacts were found by exact filename from an older pack format (`SKILL.md`, `schema.sql`, Mongo-style `db_schema.json`); the OpenAPI YAML was read as raw text |
| Naming contract had 0 entities | Same filename lookup |
| `main.py` imported a package nobody generated | Work items were generated in tree order, so `main.py` came first |
| Debugging wrote nothing in most rounds | It had to return every fixed file, complete, in one JSON reply, which was cut off with 100+ errors |
| The real blocker was invisible | `asyncpg` failed to build on the tool's Python 3.14, hidden behind the first failing check |
| Progress read as "none" | Progress counted failing checks (always 2), not errors (209 vs 109) |
| Debugging report never showed in the UI | UI read `debug_report`, agent wrote `debugging_report` |

## 2. Ground rules kept throughout

- Artifacts are found by **content**, never by filename.
- The `design_package` dict shape and the `WorkItem` contract are unchanged.
- The legacy rich-mapping plan path is byte-for-byte unchanged.
- LLM calls only through `llm_gateway`; commands only through the executor.
- A pack that can't be read degrades to the old behaviour, never worse.
- Every phase was checked on three packs: `uploads/authentication` (old format), `fixtures/resources` (e-commerce, Express) and `uploads/v1` (Fleet, FastAPI).

## 3. Phases

| # | Commit | Theme |
| --- | --- | --- |
| 0 | `14ac99e` | Measurement harness |
| 1 | `eef26a7` | Debugging agent, part 1 |
| 2 | `30ffc92` | One shape-based pack index |
| 3 | `d559436` | Pack preflight |
| 4 | `228a6ed` | Plan builder |
| 5 | `42e9592` | Naming contract and module map |
| 6 | `382693e` | Context slices and prompt caching |
| 7 | `1137340` | Python/FastAPI reconcile and integration report |
| 8 | `3f493f9` | Runtime, boot check, unverified checks |
| 9 | `a9d251a` | Debugging agent, part 2 |
| 10 | `8d2963f` | Unit-test agent |
| 11 | `7ac2f46` | First admin and README |

### Phase 0: Measurement
- `scripts/pack_report.py`: read-only report of what reaches code generation for any pack (roles, plan, per-item context size and sections, contract, requirements on no item).
- `run_fixture.py --only X` now keeps the matching items plus the files the app needs to boot (`--only-exact` for the old behaviour).

### Phase 1: Debugging agent, part 1
- Fixes are written through `write_file` and a new opt-in `edit_file` tool as they are made. A JSON reply is still accepted as a fallback.
- `app/services/check_digest.py`: splits aggregated check output per tool, counts errors (including `tsc`), groups errors by file, detects environmental causes.
- One shared `aggregate_check_results` shows every failing tool, not just the first.
- Progress is the tools' own error count. LLM exceptions become a recorded failed round.
- Report-key mismatch fixed (`debugging_report*`). Per-run greenfield `thread_id`. `LLMGateway.last_stop_reason`.

### Phase 2: One pack index
- `app/services/pack_index.py`: `load_pack` (JSON/YAML, subfolders, skips pipeline state files and binaries) and `PackIndex` with typed accessors (OpenAPI, tables with enums, conventions, tokens, validation rules, mockup sections, requirements with text, env vars, sample payloads). Cached by content hash.
- `design_pack.classify_value` classifies one in-memory artifact by shape; all five loader copies now call `load_pack`. `PyYAML` declared.
- Prompts were verified byte-identical before and after on all three packs.

### Phase 3: Preflight (normalize and warn)
- `app/services/pack_preflight.py` never edits the pack. Findings are `gap`, `normalized` or `info`; the database schema wins contradictions.
- Checks: enum drift, role spelling, invalid example UUIDs, missing endpoints, login with no way to create a user, env-name conflicts, helpers described in two files, error body, API base path, role/screen conflicts, open questions, manifest.
- Produces canonical facts (roles, enums, env vars, symbol owners, error envelope, base path, auth). Writes `reports/<project>/pack-preflight.md`, shown at plan approval and in the UI report list.
- Fleet: 3 gaps, 10 normalized, 8 info.

### Phase 4: Plan builder
- Items are ordered dependencies first (config, utils, models, schemas, repositories, services, middleware, modules, migrations, entry, manifests; frontend assets to router). Mixed directories are split; entry points move into one final `<side>-entry` item.
- Models, repositories and migrations get their tables. `_tables_for_endpoint` matches literal path segments (`/vehicle-types` no longer touches `vehicles`).
- An Alembic `versions/` folder becomes `0001_initial.py`; icon folders use the names in their description; font folders produce no fake SVGs.
- Requirement matching handles word forms and falls back to requirement text. Fleet requirement coverage 6/9 to 9/9.

### Phase 5: Naming contract and module map
- `naming_contract.py` is rebuilt on the index and the preflight facts: canonical values, every entity with fields and allowed values, every OpenAPI operation with operationId, Python handler name, body, response and roles. Fleet 0 to 7 entities, 22 to 39 endpoints; e-commerce 0 to 22 entities, 60 to 96 endpoints.
- `build_module_map(work_items)`: every importable module the plan will produce, by import name. Passed to every generation call.

### Phase 6: Context slices and caching
- `app/services/pack_context.py` (pure, per-slice budgets): parsed API operations plus transitive schemas; the pack's own DDL per table; validation rules bound by operationId, request schema or path (unbound groups are shared); requirement text; business rules; sample payloads; routes; tokens; env vars; per-page mockup screen, states and endpoints (matching is action-aware: `AdminProductNew` maps to "Admin Create Product").
- `CodeGeneratorAgent._build_context` builds three tiers (shared, item, per file). The first two travel as `LLMGateway.complete(cache_prefix=...)` with `cache_control`; `LLM_PROMPT_CACHE` setting, automatic fallback if the deployment rejects it.
- `code_generation.md` is stack-neutral and sets an order of authority (contract, module map, API, DB, conventions, mockup). Review and documentation agents find conventions by shape.
- Validation groups reaching a prompt: Fleet 1/16 to 16/16, e-commerce 5/24 to 24/24. Estimated input per full run with caching: ~0.6M (Fleet), ~1.3M (e-commerce) billed-equivalent tokens, versus 1.7M and 4.6M uncached.

### Phase 7: Python reconcile and integration report
- `app/services/python_wiring.py`, run from `reconcile_wiring`:
  - `backend.app.x` imports rewritten to `app.x` (Fleet had 102 in 37 files);
  - router imports from a package that doesn't exist pointed at the real router module;
  - prefix mounted twice dropped (`/depots/depots`);
  - routers nothing includes are mounted when they declare their own prefix;
  - missing `requirements.txt` entries added; unknown imports reported, never guessed.
- Reported (not fixed): imports of modules or names that don't exist (with where the name actually lives), `@/` imports that resolve to nothing, routes outside the pack's base path, a frontend base path the backend doesn't serve, helpers defined twice, an async engine behind sync sessions. Stored in `state.integration_findings`.
- On the real Fleet output, `import app.main` went from failing at line 1 to resolving every module import.

### Phase 8: Runtime, boot check, unverified checks
- **Environment failures** (`env_failures.py`): a check whose every failing tool failed for a reason code can't cause (missing compiler, no wheel for this Python, unreachable registry, missing tool, full disk, Windows path length or file lock, no database, installer timeout) is `inconclusive`. The run continues and records it in `state.unverified_checks`; it does not go to the debugging agent or escalate (escalation skips docs, security, PR and zip).
- **Per-project runtime** (`scripts/runtime_env.py`): a `uv` venv per backend at the project's own Python (Dockerfile, `.python-version`, `requires-python`; 3.12 fallback) under `<root>/.venvs`; a throwaway Postgres container (random port and password, labelled, swept and removed); `run_process` kills the whole process tree on timeout.
- **Boot check**: `Executor.boot()` starts the FastAPI app or Node server against the throwaway database (project migrations, else the pack's DDL) and requires an HTTP answer below 500. It runs after compile and build unless a backend install failed.
- `npm install` is skipped while the manifests are unchanged.
- **Scaffold**: compose database credentials and healthcheck, `service_healthy` dependency, frontend volume and install; `.env.example` lists every variable the pack declares (secrets blank); driver follows the pack DSN (`asyncpg`); `reconcile_scaffold_manifests` restores the test script and harness devDependencies a generated `package.json` dropped.
- Verified for real on the Fleet output: Python 3.11 venv, requirements installed, throwaway Postgres, project migrations applied, uvicorn started; it then failed on a genuine code defect, now reported.

### Phase 9: Debugging agent, part 2
- Naming contract, module map and conventions are the round's cacheable prefix. The prompt adds environment facts (`Executor.environment_facts`), unverified checks and the integration findings.
- `_batch`: up to 12 files per round, drawn across failing checks (boot, test, build, compile), each with its own error lines; a traceback's innermost project frame leads with its exception.
- `state.debug_history`: what each round changed and whether the count fell; files edited in 3+ rounds without progress are called out.
- `DEBUG_CAP` 10 to 5. Tool loops now cache the prefix and a rolling breakpoint on every turn.

### Phase 10: Unit-test agent
- One call and one test file per testable source file, at a unique path. Prompts carry the contract and module map (shared), then sources, requirement text, validation rules and sample payloads (per item), plus a Test environment section (`TestClient` with `dependency_overrides`, database state, one self-contained file per reply).
- Files with nothing to test (package markers, types, configs, migrations, DOM-mount entries) are skipped and never count as a failure.

### Phase 11: First admin and README
- `pack_preflight.bootstrap_plan` is the single answer for packs with a login and no way to create a user (Fleet): seed path, command, user table, canonical admin role, `ADMIN_EMAIL`/`ADMIN_PASSWORD`.
- The plan adds a `backend-seed-admin` item before the entry point; the scaffold declares the variables; the README gets real commands (`cd`, copy `.env`, `alembic upgrade head`, seed, run), a First login section, the full variable list and "Open questions in the design".

## 4. Results

| Measure | Before | After |
| --- | --- | --- |
| Fleet requirements on a work item | 6/9 | 9/9 |
| Fleet naming contract | 0 entities, 22 endpoints | 7 entities, 39 endpoints |
| Fleet validation groups in prompts | 1/16 | 16/16 |
| Entry points generated last | no | yes (both packs) |
| Fleet `main.py` import | fails at line 1 | all module imports resolve |
| Tests | 668 passed | 1,145 passed |

Known baseline failures are unchanged: 7 failed plus 1 error, all from design-pack fixtures that no longer exist (`test_plan_and_manifest.py`, one in `test_imp001.py`).

## 5. Not done / open

1. **No real model run** since Phase 1. Still unconfirmed: that Foundry accepts prompt caching, that the boot check passes on a real generated backend, and the real token cost.
2. `unverified_checks` appear in the run summary only, not the README or PR (the README is written before the run).
3. `SDLC_Frontend` is untouched: add `debugging` and `pack-preflight` to `ReportKind` / `ICON` / `STAGE_REPORT_KIND` (that repo has uncommitted work of its own).
4. The branch is not pushed or merged.

## 5b. Suggested next step

Run: `scripts/run_fixture.py uploads/v1 --only auth --no-publish --fresh -y`, then check the boot result, the debugging report and the logged cache read/write token counts.
