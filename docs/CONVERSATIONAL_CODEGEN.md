# Conversational code generation — what was built

Complete record of the feature: what it does, how it works, every file, every deviation from the
plan, every bug found, and how to verify it. This supersedes the working plan document.

| | |
|---|---|
| **Backend branch** | `feature/conversational-code-generator`, off `main` @ `ba1607a` (repo `Patil-Khushi/SDLC_Implementation`) |
| **Frontend branch** | `feat/conversational-chat-panel`, off `feat/refactoring-edits-page` (repo `Patil-Khushi/SDLC_Frontend`) |
| **Status** | All planned steps complete. One planned change dropped after its premise proved wrong. |
| **Committed** | **No.** Both branches have zero commits; everything is in the working tree. |
| **Backend suite** | `7 failed, 811 passed, 13 skipped, 1 error` (~65s). The 7 + 1 are pre-existing. Baseline before this work: `7 failed, 668 passed`. |
| **New tests** | 11 files, **145 passing** |
| **Size** | Backend: 8 new modules/prompts (~1,250 lines) + 672 insertions across 10 tracked files. Frontend: 1 new component (213 lines) + 126 insertions across 3 files. |

## The problem

The Code Generator Agent was a one-shot batch. `build_plan(pack)` decomposed a design package into
~59 work items **once**; the loop `select → code_generator → gate → feature_publish → select`
walked that list **once**; and the plan was frozen the moment `workflow.invoke` was called. The only
human gate, `run_fixture._confirm_plan`, ran *before* the graph started.

Worse, `code_generator` called `self.llm.complete`, expected strict JSON
`{"files":[{"path","content"}]}`, and **never read a file** — so it could only ever *create*.

A changed requirement therefore meant killing the run and starting a 3–6 hour build over.

## The idea

**Make the plan a living object, and teach the generator to modify as well as create.**

A chat message never edits files. It produces a *plan patch* — add / modify / remove work items —
which the existing loop picks up at the next work-item boundary.

Two properties of the existing code made this tractable without re-architecting anything:

1. **The loop already re-reads the plan.** `select_work_item_node` re-reads `state["work_items"]`
   and re-compares `work_item_index < len(items)` on *every* pass. So changing the pending tail
   mid-run needs no `interrupt()`, no `update_state()`, and no pause.
2. **`WorkItem` already modelled change.** It carried `action: create|modify|delete`,
   `change_intent` and `acceptance_criteria` — all optional with defaults. The model is
   **unmodified**, so the published contract schemas cannot drift and `test_contracts.py` still
   passes.

### Scope boundary

**This is entirely inside the Code Generation slice.** It does not use or extend the brownfield lane
(`acquire` / `change_plan` / `code_modifier` / `change_gate` / `change_verify`,
`source_mode="brownfield"`) — that lane exists for cloning an *external* repo and is a separate
feature with different safety rules.

Verified mechanically: no new module imports any brownfield service, and no brownfield file is
modified. The one shared import is `app/agents/editing.py`, which is not brownfield code — it is the
write fence the Refactoring agent already uses.

## How it works

### 1. Acceptance is decoupled from application

`POST /api/chat` appends one `fsync`'d JSONL line and returns `202`. It takes no lock the graph
holds, touches no `WorkflowState`, and never waits on the run.

That split is the entire reason a request can be sent **while work item 23 of 59 is blocked inside a
multi-minute LLM call**. The executor and LLM gateway are process-global singletons and
`graph_server` serialises runs behind `_run_lock`; an endpoint that touched any of it would block
for hours. Application happens later and elsewhere, at a work-item boundary.

The inbox lives under `app/workspace/chat/`, deliberately **outside** the generated project,
because `graph_server._drive` does `shutil.rmtree` over `generated-apps/<project>` — including its
`.git` — before every real run.

### 2. A ledger of what was actually written

Each work item's real output is recorded as `{work_item_id, feature_id, requirement_ids, files,
action}`. This is what makes "change the login flow" resolve to a handful of files instead of a
whole-project regeneration.

It records **observed** files, deliberately not the pack's predicted `target_files` — the two
routinely differ, and an incremental edit aimed at the predicted set would miss the real code.

### 3. The patch, applied deterministically

`chat_intake_node` drains the inbox on the existing `feature_publish → select` edge, asks
`ChatPlannerAgent` for a structured patch, and applies it in plain code.

The division of labour with the model is the safety property: the planner proposes **ops over
work-item ids** and prose intent, and **never names a file path** for a modify or remove. Paths are
resolved afterwards from the ledger. A planner that could name paths could reach any file in the
project from one sentence; a planner that can only name work items can only ever touch files some
known work item produced.

| Request | Item not generated yet | Item already generated |
|---|---|---|
| **add** | append `WorkItem(action="create")` | same |
| **modify** | rewrite its `change_intent` in place (nothing on disk to edit yet) | append `WorkItem(action="modify")` over its ledger files |
| **remove** | drop it from the pending tail | append an unwire `action="modify"` item; record orphans |

**The invariant everything rests on: only `work_items[cursor:]` may be touched.**
`select_work_item_node` advances `work_item_index` to `index + 1` after selecting, so every index
below the cursor is already generated, gated, committed and possibly pushed. Rewriting one would
regenerate published work; renumbering the tail would skip an item outright. The cursor itself is
never moved by the patch.

Nothing in the patch path raises. A message typed mid-run must not be able to kill a multi-hour
build, so unknown ids, malformed items and unlocatable requests all come back as report lines. A
**refusal is a successful outcome** — declining a request that is too vague to locate, a rename (no
call graph, so no way to find every caller), or a cross-cutting sweep is correct behaviour.

### 4. The generator gained a modify mode

The one real agent change. When `current_work_item.action == "modify"`, generation goes through
`self.llm.complete_with_tools` bound to `editing_tools(executor, project_dir, touched,
allowed_paths=item.target_files)`, so the model **reads the existing file** and edits it in place.
`action == "create"` takes today's exact path — `complete`, strict JSON, whole-file write.

`allowed_paths` is the only thing standing between a conversational request and an edit to a file
nobody planned or reviewed, so it is passed for every modify item and never widened. Reads stay
unrestricted: understanding a change means reading its callers.

### 5. The gate stopped being vacuous

`files_complete` — the only code-generation gate — asks whether the target *exists*. For a create
item that is the right question. For a modify item **the file already exists**, so the gate passed
whether the agent rewrote the file or did nothing at all. A no-op edit would be committed, pushed,
and reported as an applied change: the worst failure available to a conversational loop, because the
user is told their request landed.

So `select_work_item_node` snapshots the targets' sha256 digests when it selects a modify item, and
`gate_node` adds a `files_changed` check that fails when nothing moved. It flows through the
existing repair/escalate machinery untouched.

Deliberately **not** a whole-tree collateral scan: writes on this path are already fenced at the
tool layer, so hashing an entire generated project per item would cost time proportional to project
size and prove nothing the fence has not already guaranteed. A comment in `_files_changed_check`
says so, to stop a future reader "fixing" it.

### The flow

```
   chat, out of graph ──► POST /api/chat ──► chat_inbox.append() ──► 202 + queue position
                                                    │  (no lock, no state write)
   design package                                   │
        │                                           ▼
   build_plan ──► work_items ──►┌────────────► select ──┬── no items left ──► reconcile ──► commit
                                │                 │     │                      (then review / test /
                                │           current item│                       security / finalize)
                                │                 ▼     │
                                │         code_generator│
                                │        ┌──────────────┴──────────────┐
                                │        │ action=create               │ action=modify   ◄── NEW
                                │        │ llm.complete, strict JSON   │ llm.complete_with_tools
                                │        │ whole-file write            │ + editing_tools(allowed_paths)
                                │        │ (unchanged)                 │ reads, then edits in place
                                │        └──────────────┬──────────────┘
                                │                       ▼
                                │                     gate
                                │        files_complete  +  sha256 changed?  ◄── NEW for modify
                                │                       │
                                │            fail & <3 ─┴─► repair ──► gate
                                │            fail & =3 ────► escalate (reports undrained requests)
                                │                pass
                                │                  ▼
                                │           feature_publish   (commit + push dev)
                                │                  │
                                │                  ▼
                                └──────────  ★ chat_intake   ◄── NEW
                                             drains inbox; asks the planner for a
                                             structured patch; applies it to
                                             work_items[work_item_index:]
                                                   │
                                                   └──► select
```

### One turn, end to end

Mid-build at item 23 of 59, the user types *"drop the wishlist and make login use OTP"*. The POST
returns in milliseconds and a line appears in the dashboard. Item 23 finishes untouched and
publishes. `chat_intake` then drains the message: the three not-yet-started wishlist items are
dropped from the pending tail and never generate; login was generated at item 9, so a new
`action="modify"` item is appended over its ledger files. The loop continues, reaches that item,
reads the existing login files, edits them in place, and the gate proves the targets changed. The
plan count updates from 59 to 57.

## Files

### Backend — new

| Path | Lines | Tests | Purpose |
|---|---|---|---|
| `app/services/chat_inbox.py` | 184 | 18 | Append-only JSONL FIFO; heals a crash-truncated tail; `high_water()` for run-start seeding |
| `app/services/requirement_ledger.py` | 142 | 11 | Pure functions over `list[dict]`; observed files, not predicted |
| `app/services/plan_patch.py` | 261 | 21 | Deterministic add/modify/remove; never raises; pending-tail only |
| `app/services/codegen_digest.py` | 84 | 9 | sha256 proof a modify item moved its targets |
| `app/agents/chat_planner.py` | 194 | 15 | The one LLM call; proposes ops over ids, never a path |
| `app/prompts/chat_patch.md` | 81 | — | Structured ops only; refusal rules |
| `app/prompts/code_generation_modify.md` | 59 | — | Read-before-write, smallest-diff, preserve the public surface |
| `scripts/run_chat.py` | 242 | — | Drives a turn in seconds instead of a 3–6 hour run |
| `docs/CONVERSATIONAL_CODEGEN.md` | — | — | This file |

Test files: `test_chat_inbox` (18), `test_requirement_ledger` (11), `test_codegen_digest` (9),
`test_plan_patch` (21), `test_chat_planner` (15), `test_codegen_modify` (11), `test_chat_intake`
(16), `test_gate_files_changed` (8), `test_chat_api` (14), `test_graph_server_gateway_restore` (7),
`test_tag_reqs` (15). **145 total.**

### Backend — changed

| Path | Change |
|---|---|
| `app/agents/code_generator.py` | Modify mode via `complete_with_tools` + `editing_tools`; ledger rows in `_record_success`; create path untouched |
| `app/graph/nodes.py` | `chat_intake_node`; digest snapshot in `select`; the `files_changed` gate check; `escalate_node` reports undrained requests |
| `app/graph/graph.py` | One new node; `feature_publish → chat_intake → select` |
| `app/graph/router.py` | `CHAT_TURN_CAP = 20` |
| `app/graph/state.py` | 10 additive `total=False` keys, all plain builtins; `new_state(chat_enabled=False)` |
| `app/config/settings.py` | `chat_enabled` (default **False**), `chat_turn_cap` |
| `app/services/design_pack.py` | `_build_tag_reqs`: token-exact matching, unambiguous-only fallback, unresolved tags reported |
| `scripts/graph_server.py` | `POST /api/chat`, `GET /api/chat/log`, the `chat` SSE event, live `plan_count`, node-stage map entries, `dry_run_gateway()` |
| `scripts/run_fixture.py` | Reads `settings.chat_enabled`; seeds `chat_seq` from `high_water()` |
| `app/tests/test_state.py` | New keys in the two shape assertions; one new test |

### Frontend — branch `feat/conversational-chat-panel`

| Path | Change |
|---|---|
| `src/components/dashboard/ChangeRequestPanel.tsx` | **New**, 213 lines. Textarea + send, a queued list, and an applied-turns list showing the plan delta per turn |
| `src/lib/api.ts` | `chat` on the `RunEvent` union; `ChatTurnReport` / `ChatPlanDiff` / `ChatMessage` / `ChatTurn`; `sendChat()` and `fetchChatLog()`. Still the only place the app fetches. |
| `src/store/runStore.ts` | `chatTurns` state; a `chat` case that refreshes `planCount` and pushes a **human-attributed** timeline entry |
| `src/pages/DashboardPage.tsx` | Mounts the panel above `TerminalLogs` |

The input is deliberately **not** gated on run status, because acceptance does not touch the run.
`planCount` is *refreshed*, never incremented: one turn can both add and remove items, and the stage
tiles read that number. A refusal renders as information, not as an error.

## Backward compatibility

`chat_enabled` defaults to `False`, and with it off `chat_intake_node` returns state untouched — so a
run is byte-identical to one from before the feature existed. An `action="create"` work item takes
the original strict-JSON path with no behavioural change, asserted directly by
`test_the_create_path_also_records_the_ledger_and_is_otherwise_unchanged`.

`WorkItem` is unmodified, so the published `contracts/implementation-to-testing/*.schema.json`
cannot drift.

## Deviations from the plan

Five, all deliberate:

1. **`app/services/codegen_digest.py` is new.** The plan left "reuse `change_gate`'s digest helpers
   or write a local one" open. Resolved as **local**, to keep the slice free of brownfield coupling.
2. **`app/agents/chat_planner.py` is new.** The plan put the patch-proposal LLM call inside
   `chat_intake_node`, which violates standing rule 4 — nodes never call the LLM directly.
3. **`app/prompts/code_generation_modify.md` is new.** Modify mode needs a different system prompt.
   Deliberately not named `code_modification.md` — that belongs to brownfield.
4. **Chat is during-generation only**, the plan's option (a). See Limits.
5. **No whole-tree collateral scan in the gate**, for the reason given above.

## Dropped

**`app/agents/unit_test.py` — no change made.** The plan claimed a removed work item would flip
`tests_ok` false and escalate the run. It does not: the existing "no readable source" skip
(`unit_test.py:146-163`) already handles an item that never produced files, and
`tests_ok = bool(written) or not work_items` cannot go false from one skipped item. No change is
needed, so none was invented.

## Bugs found and fixed

Seven. Four were caught by tests written for this feature; three were pre-existing.

**Introduced during this work, caught by its own tests:**

1. `chat_inbox.append` after a crash-truncated line spliced the new message into the partial record,
   losing the good message as well as the broken one. Now heals a missing final newline first.
2. `scripts/run_fixture.py` used `get_settings()` without importing it — a `NameError` on every run.
3. A no-op modify recorded a ledger row with zero files, making `is_generated` true for an item that
   produced nothing; a later modify of it would resolve to no files and be skipped as "the ledger
   records no files for it", permanently. Rows are now written only when something was written.
4. The gateway restore re-assigned a bound method read from the class, leaving a permanent redundant
   instance attribute. It now deletes when there was no prior override and restores an existing one —
   which matters because several test modules inject their own via `monkeypatch`.

**Pre-existing:**

5. **The dry-run gateway leak** (plan step 0). `graph_server._drive` patched the process-global LLM
   gateway for dry-run and never restored it. Every agent binds that singleton at construction, so
   after **one** dry-run every later *real* run in the process silently emitted canned placeholder
   files, and `/api/plan/revise` returned canned JSON. Restarting the server was the only reset. Now
   a `dry_run_gateway()` context manager, restored in the same `finally` as the logger.
6. **Requirement-ID over-matching.** `_build_tag_reqs` matched a singularised tag stem as a
   substring of the glued feature name and gave the tag the **union of every feature it loosely
   hit** — a tag silently inheriting two unrelated features' requirements. Now token-exact first,
   with a loose match accepted only when unambiguous. Abbreviations still work (`auth` →
   "Account & Authentication", which the live pack depends on).
   The fix also made visible what was silent: on the live pack **7 tags map to no requirements at
   all** — `addresses, promo-codes, refunds, returns, search, serviceability, users`. Work items for
   those endpoints carry `requirement_ids=[]`. That is a design-pack gap, not a code bug, and it is
   now logged rather than invisible. All 8 working mappings are unchanged.
7. **Inbox replay across runs.** The inbox is durable and outlives the run, so a *fresh* run of a
   project whose inbox already held earlier requests would start at `chat_seq=0` and re-apply the
   entire conversational history to the new build — a months-old "drop the wishlist" silently
   patching a fresh plan. Both drivers now seed `chat_seq` from `chat_inbox.high_water(project)` at
   run start, so a run only sees messages sent **while it was running**. `--resume` restores the
   checkpointed value and is unaffected.

## Limits

- **Chat works during generation only.** `chat_intake` sits inside the item loop. Once the plan is
  exhausted the run proceeds `reconcile → commit →` review / test / security `→ finalize → package
  → END`, and that back half can run a long time with no way to absorb a message. A later request
  needs a new run. Widening it means either looping the back half back to `select`, or making each
  post-build turn its own short graph invocation on the same `thread_id`.
- **`naming_contract` drift.** A chat turn does not amend the design package, so `naming_contract` —
  prepended to *every* generation call — keeps describing the original design while the code moves
  on. This is the deepest limitation in the design.
- **Deletion is soft.** `remove` never deletes a file: there is no `Executor.delete_file`, and
  per-feature commits may already be pushed. A pending item is dropped; a generated one gets an
  unwire item, and its files are listed under `orphan_files`.
- **A tag inherits its whole feature's requirements**, so `cart` and `checkout` both map to all of
  "Cart & Checkout". Splitting them needs per-requirement tagging the design pack does not carry.
- **One run at a time, unchanged.** Chat works *within* the single run, which is why acceptance had
  to avoid `_run_lock` entirely.
- **`CHAT_TURN_CAP` is not a budget.** Each turn costs one planner call plus whatever it
  regenerates. Hitting the cap is not an error and does not escalate — intake stops draining.
- **No `interrupt()`.** `from langgraph.types import interrupt, Command` does import on the
  installed langgraph, so the mechanism exists — but a node's pre-`interrupt` half re-executes on
  resume, which cuts against this codebase's "nodes mutate state and return it" idiom, and it would
  give `--resume` two incompatible meanings. The out-of-graph inbox gets the same result with no new
  discipline.

## Open decisions for the team

- **Should a chat turn amend the design package?** Not doing so causes the `naming_contract` drift.
  Doing so crosses the Design-team boundary, and `AGENTS_CONTEXT.md` §11 forbids inventing contract
  fields — so this needs agreement, not a unilateral choice.
- **Is soft delete sufficient**, or should `Executor.delete_file` be added? If added it must be
  absent from `get_repair_tools()` and callable only from a fixed, non-LLM node.
- **Widen chat past generation** — which of the two options above.
- **The 7 unmapped OpenAPI tags** are a design-pack gap to raise with the Design team.

## Verification

```powershell
cd SDLC_Implementation

# Full suite. Expect: 7 failed, 811 passed, 13 skipped, 1 error (~65s).
# The 7 failures + 1 error are the KNOWN pre-existing test_plan_and_manifest.py /
# test_imp001.py fixture problem (design packs that no longer exist anywhere).
./.venv/Scripts/python.exe -m pytest app/tests -q

# The eleven new test files: 145 passed.
./.venv/Scripts/python.exe -m pytest `
    app/tests/test_chat_inbox.py app/tests/test_requirement_ledger.py `
    app/tests/test_codegen_digest.py app/tests/test_plan_patch.py `
    app/tests/test_chat_planner.py app/tests/test_codegen_modify.py `
    app/tests/test_chat_intake.py app/tests/test_gate_files_changed.py `
    app/tests/test_chat_api.py app/tests/test_graph_server_gateway_restore.py `
    app/tests/test_tag_reqs.py -q

# Nothing here may disturb these.
./.venv/Scripts/python.exe -m pytest app/tests/test_code_generator.py app/tests/test_graph.py `
    app/tests/test_node_stage_map.py app/tests/test_brownfield_acquire.py `
    app/tests/test_contracts.py app/tests/test_state.py -q
```

End to end against an existing project, no model calls:

```powershell
./.venv/Scripts/python.exe scripts/run_chat.py ../generated-apps/live-proof-auth `
    --dry-run --show-plan --apply --say "make login use OTP"
```

Observed: the message is queued, the planner proposes a `modify`, `plan_patch` resolves the target
from the ledger and appends `WI-001-m1`, the plan goes 6 → 7, and the gate reports
`files_complete` **ok** / `files_changed` **FAIL** — the vacuous-gate hole closing on a no-op edit.
(In `--dry-run` nothing drives the editing tools, so the no-op and the failure are correct.)

Drop `--dry-run` for a real turn (needs `ANTHROPIC_FOUNDRY_*` in `.env`).

Frontend:

```bash
cd SDLC_Frontend
npm run typecheck && npm run build     # both pass
npm run dev                            # :5173, expects the bridge on :8200
```

The HTTP contract was checked against a live server, since `tsc` cannot verify field names across an
HTTP boundary: `POST /api/chat` → `{"seq":1,"queued":1,"project":"..."}`; a second message → `seq
2`; blank text → `400`; `GET /api/chat/log` → `{available, project, messages[{seq,text,project,
received_at}], consumed_seq, turns}`; unknown project → `available:false` with a reason. Every field
matched the TypeScript interfaces.

## Also done in the same session (unrelated to this feature)

- **`main` pulled** in `SDLC_Implementation`: fast-forward `71394fd` → `ba1607a`, six commits
  (PRs #23/#26/#27/#29/#30/#31), no merge commit, working tree untouched.
- **The workspace `CLAUDE.md` audited** against the code — 16 corrections. The notable ones: it
  claimed `.venv/` was checked in (it is gitignored, `git ls-files .venv` → 0); the test baseline
  said 472 passed (it was 668); it said "Four safety properties" then listed five; and it pointed at
  `app/main.py` for `POST /implementation/start`, which actually lives in `app/api/routes.py`. Also
  added: `docs/BROWNFIELD.md` to the doc-precedence chain, the six brownfield nodes mapping to
  `None` in `_NODE_TO_STAGE`, and `finding_aggregator.py` as a fifth deterministic no-LLM pass.
