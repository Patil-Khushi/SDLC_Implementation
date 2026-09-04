# Brownfield mode — implementing a change request against an existing codebase

Until now this service could only do one thing: take a design package and build a **new**
application from scratch, then publish it to a **new** GitHub repo it created itself. Brownfield
mode is the other half of the job — *here is a repository that already exists, here is a change we
want* — and it is the more common real-world case.

Status: **built and working end to end, locally.** One thing is still untested against live
GitHub; see [What is verified](#what-is-verified) before relying on it.

---

## Running it

```powershell
# Edits and commits LOCALLY. Nothing reaches the target repo.
./.venv/Scripts/python.exe scripts/run_change_request.py https://github.com/owner/repo `
    --change-request cr.md

# Also push the branch and open a DRAFT pull request.
./.venv/Scripts/python.exe scripts/run_change_request.py https://github.com/owner/repo `
    --change-request cr.md --push
```

`--push` is **opt-in**. The first flag anyone reaches for is the one that writes to their
repository, so the default run edits and commits locally and opens nothing — the diff is still
there to read in the workspace.

The change request is JSON, or plain Markdown (first heading becomes the title):

```markdown
# Reject an empty secret key

`Signer.__init__` accepts an empty string as the secret key, which silently produces
signatures anyone can forge. It should raise `ValueError`.

## Acceptance criteria
- Constructing a Signer with an empty secret key raises ValueError
- A non-empty secret key behaves exactly as it does today
```

Requires: `git` on PATH, a **public** `https://github.com/<owner>/<repo>` URL, and
`ANTHROPIC_FOUNDRY_*` in `.env`. No Docker.

---

## The lane

One graph, two entry lanes. `router.route_entry` is the only place they diverge — anything that is
not explicitly `source_mode == "brownfield"` (including an absent field) takes the greenfield lane,
so every existing caller is byte-identical.

```
START ──route_entry──> { scaffold (greenfield)  |  acquire (brownfield) }

acquire → change_plan → select ⇄ code_modifier → change_gate → (retry ≤3)
        └ no items left → change_verify → change_commit → finalize (DRAFT PR) → package → END
```

| Node | Does |
|---|---|
| `acquire` | validate URL → clone → resolve the real default branch → pin base commit → cut `sdlc/cr-<run>` → survey → baseline digests → **run the repo's tests** |
| `change_plan` | LLM proposes which files change; then a deterministic validator checks that proposal against the repo actually cloned |
| `code_modifier` | implements one work item through a read-then-write tool loop, fenced to that item's targets |
| `change_gate` | proves the targets changed **and that nothing else did** |
| `change_verify` | re-runs the repo's own suite and compares to the baseline |
| `change_commit` | commits the change set by explicit path |
| `finalize` | opens a **draft** PR against the repo's discovered default branch |

---

## The five safety properties

These are the load-bearing part. Change any of them and re-read this section.

### 1. It never reaches `scaffold_node`

`scaffold_node` unconditionally overwrites `.gitignore`, `README.md`,
`package.json`/`requirements.txt`, `Dockerfile`, `docker-compose.yml` and the jest/babel configs
from `DEFAULT_CAPABILITIES` — never reading disk — then calls `publish_scaffold`, which does
`git checkout -B main` → commit → `git push -u origin main`.

**Against a clone that push is a clean fast-forward onto the user's default branch**: their
manifests replaced, committed, pushed, no PR, no review.

Brownfield routes *around* it rather than guarding *inside* it. A guard would leave the landmine
armed for whoever adds the next write; routing around it cannot regress. The regression test
asserts the absence:
`test_brownfield_acquire.py::test_brownfield_run_through_the_graph_never_scaffolds`.

A second chain in the same area: the rendered `.gitignore` template is 15 lines of Python/node
basics — no `*.pem`, `.env.local`, `terraform.tfstate`. Replacing a real repo's ignore rules
un-ignores whatever they protected, and `publish_sweep`'s `git add -A` would then stage it. So
brownfield also **never calls `publish_sweep`** — `change_commit` stages an explicit path list.

### 2. The gate proves the change happened — and only the change

`files_complete`, the only gate the pipeline had, asks whether every target path exists. In
brownfield every target already exists, so it passes whether or not the agent wrote anything: a run
could report success with a zero-line diff.

`change_gate.py` compares sha256 digests against the pre-edit baseline and fails on both:

- **the target did not change** — `modify` requires the content to differ;
- **a file no work item claimed did change** — the blast-radius check.

The second is the one with teeth. Whole-file overwrites make collateral damage easy — the model
reads three files to understand one and rewrites all three — and a reviewer skimming a large diff
will not reliably catch it. Nothing else in the pipeline would.

It writes the same `GateResult` shape `gate_node` writes, so the existing repair accounting,
routing and escalation work over it unchanged.

### 3. Writes are fenced to the validated plan

`editing_tools(allowed_paths=...)` refuses any write outside the current work item's targets, and
the refusal goes *back to the model* as the tool result so it can correct course. The plan was
validated file-by-file; an edit outside it would be an unreviewed change wearing an approved plan's
authorization.

Refactoring passes no fence — its findings list is its own authorization. **That is why the shared
quality pipeline is not on this lane** (see [Known limits](#known-limits)).

### 4. The PR is a draft, on its own branch, with a safe body

- **Branch:** `sdlc/cr-<run>`, never `dev` (on a real repo usually a shared integration branch,
  where committing would land on someone else's work and make the PR diff every unrelated commit
  already on it).
- **Base:** the repo's *discovered* default branch — `master`, `develop`, whatever it is. Not a
  hardcoded `main`, which would fail at the very last step of a run that already did all its work.
- **Draft:** it cannot be merged until a person marks it ready. With no call-graph analysis, "a
  human must look at this" is the actual safety model.
- **Body:** the change request, the files changed, and what else imports them — **not** the security
  report. In brownfield that describes the user's *own pre-existing code*, and this PR may be public.

### 5. Verification uses the repo's own tests, and never overclaims

`Executor.test()` cannot be used here. It gates pytest on `requirements.txt` and npm on
`package.json`, so a repo packaged with `pyproject.toml` — most Python projects since ~2021 —
matches nothing and it returns `passed=True` **having run nothing**. A vacuous green is worse than
no check: the run claims verification that never happened.

`test_command.py` detects the real command from the repo's own manifests, and its three-way outcome
is load-bearing:

| Status | Meaning | Blocks? |
|---|---|---|
| `passed` / `failed` | the suite ran | only a **regression** blocks |
| `inconclusive` | the suite **could not run** — missing dev dep, no tests collected, tool absent, timeout | never |

**`inconclusive` must never collapse into `failed`.** A repo whose dev dependencies are not
installed would otherwise report "your change broke the tests", blocking a correct change and
sending the model off fixing code that was never broken.

Only a *regression* — passed before, fails now — stops the commit. A repo arriving with a red suite
stays changeable. An unverified run still commits, but the report and the PR say **"NOT
test-verified"** in as many words.

---

## New modules

| Path | LLM? | What |
|---|---|---|
| `app/services/repo_inventory.py` | no | survey a clone: source vs tests vs config, modules, language mix, package managers, digests, default branch |
| `app/services/change_plan.py` | no | validate the planner's proposal; `normalize_target` is the single canonical path form |
| `app/services/change_gate.py` | no | proof-of-change + blast radius + regression classification |
| `app/services/test_command.py` | no | detect and run the repo's own suite; three-way outcome |
| `app/services/wiring.py` (added to) | no | `build_import_graph` / `dependents_of` — file-level "who imports this", Python **and** JS |
| `app/agents/editing.py` | — | the shared read/write tool loop, with the truncation and fence guards |
| `app/agents/change_planner.py` | yes | browses the repo with read-only tools, proposes a plan |
| `app/agents/code_modifier.py` | yes | implements one work item |
| `scripts/run_change_request.py` | — | the driver |

The deterministic services join the existing `naming_contract` / `wiring` / `plan_builder` family:
no model, same input same output, unit-testable without a sandbox, and **degrading to empty rather
than guessing**. An empty inventory is a legible stop; a guessed one would send the planner after
files that do not exist.

### `WorkItem` gained three optional fields

`action` (`create`/`modify`/`delete`), `change_intent`, `acceptance_criteria` — all defaulted, so
old design-pack payloads still validate under `extra="forbid"` and mean exactly what they always
did. Regenerate schemas with `python -m app.models` after touching them.

---

## What is verified

Run against `pallets/itsdangerous` with a real change request:

- **Refusal works.** Asked to add `max_age` to `Signer.unsign()`, the planner read `signer.py`,
  `timed.py` and `exc.py` and refused — `TimestampSigner` already has it, and retrofitting the base
  class would change the wire format and break the subclass. Correct about the library.
- **A real change works.** Asked to reject empty secret keys, it produced a 4-line guard in
  `Signer.__init__` and a 5-line parametrized test matching the file's existing style. Verified by
  hand: `Signer("")`, `Signer(b"")` and `Signer([""])` all raise; a valid key still signs. The
  repo's own suite: **69 passed**.
- **Containment holds.** Exactly the two planned files changed, working tree clean, `main`
  untouched, nothing pushed.

Test suite: **665 passed, 7 failed, 13 skipped, 1 error.** The 7 + 1 are pre-existing and unrelated
— they need design-pack fixtures that no longer exist anywhere (see the workspace `CLAUDE.md`).

Graph-level, through the compiled graph with everything faked:

| Scenario | Outcome |
|---|---|
| Suite passed before, fails after | **no commit, no push, no PR** — escalates |
| Suite already red, still red | commits — "not caused by this change" |
| Suite green before and after | commits, verdict `passed` |
| Suite cannot run | commits, PR says **NOT test-verified** |
| Edit changed nothing | never reaches a commit |
| Model writes outside its targets | refused, told why |
| Truncated whole-file rewrite | refused, file left intact |

### Not verified

**No live `--push` run has ever been done.** The draft-PR path is tested at graph level with
`FakeGitHubClient` — correct branch, base, `draft=True`, body — but it has never talked to GitHub.
That needs a scratch repo and about five minutes.

---

## Bugs found along the way

Three were found by *running it for real*, not by the tests — worth recording, because each one
looked fine in unit tests.

1. **`env=` replaces the environment, it does not extend it.** Passing a bare dict of git switches
   stripped `PATH`/`SystemRoot`, so git could not resolve DNS and failed with
   `Could not resolve host: github.com` on a machine whose network was fine.
   (`local_executor._pat_env` merges with `os.environ` for exactly this reason.)

2. **Bare `python` resolved to the wrong interpreter** — a base install without pytest, rather than
   the venv. `LocalDiskExecutor.test()` already used `sys.executable`; the new detector did not.

3. **`python -m pytest` with pytest missing exits 1** — the same code a real test failure uses — so
   the classifier reported *"the tests failed"* when nothing had run. This is precisely the
   collapse `test_command.py` exists to prevent, and it survived 18 unit tests because every one of
   them fed the classifier an exit code chosen by hand.

Two more came from an adversarial review of the finished code (14 findings, 9 refuted, 5 confirmed):

4. **`validate_plan` checked the un-normalized path.** `_escapes_project` normalized internally but
   discarded the result, so the anchored forbidden-path patterns and the existence check both ran on
   the raw string: `src/../.github/workflows/ci.yml` was **accepted**. A bypass in the one component
   whose entire job is to stop it. Found independently by two reviewers.

5. **An `import` inside a docstring invented a dependency edge** — a usage example in a module
   docstring became a phantom caller in the blast-radius report. Under-reporting a dependent is bad;
   asserting one that does not exist is worse, because it is indistinguishable from a real one.

---

## Known limits

- **No call-graph analysis.** The import graph is file-level — it answers "who imports this file",
  not "does this edit break that caller". The planner is instructed to *refuse* renames, signature
  changes and moves for this reason, and the PR body states the limit explicitly.
- **v1 scope is localized changes**: ≤5 files, ≤5 work items. Cross-cutting requests, migrations
  and anything too vague to locate are refused with a stated reason. **A refusal is a success.**
- **Public repos only.** The clone allowlist is `https://github.com/<owner>/<repo>` — an SSRF guard
  that matters more here than anywhere, because the host clone runs with the operator's own git
  credentials and, until brownfield, nothing ever handed it a caller-supplied URL.
- **`LocalDiskExecutor` only.** The exec-sandbox has no egress to github.com, so it can neither
  clone the target nor push the result. Rejected at the boundary, not 40 minutes into a run.
- **The shared quality pipeline (code review → refactoring → debug/test → security) is not on this
  lane.** Not only because it needs Docker: `RefactoringAgent` has no `allowed_paths` fence, so it
  would edit files outside the validated plan and break the containment property in §2/§3.
  Fencing it is the prerequisite.
- **Whole-file overwrites, with a 60,000-char ceiling.** Larger files are refused loudly rather than
  truncated. An anchored search/replace would remove the ceiling.
- **`git_token` lives on `WorkflowState`** and is checkpointed to SQLite — now carrying write access
  to a repository the service does not own. The interim control is a fine-grained PAT scoped to one
  repo with `contents:write` + `pull_requests:write`. This is an accepted risk, not a fix.

---

## Next

1. **One live `--push` run** against a scratch repo — the only part of the design never exercised
   for real.
2. **Fence `RefactoringAgent`** with `allowed_paths`. Small change; unblocks putting brownfield
   through the shared quality pipeline.
3. **`patching.py`** — anchored search/replace. Removes the file-size ceiling, and stops whole-file
   rewrites being a collateral-damage risk at all. Improves greenfield refactoring too.
4. **Move `git_token` off state** into a process-scoped credential provider.
5. The API (`POST /implementation/start`) and frontend surfaces — deferred deliberately.
