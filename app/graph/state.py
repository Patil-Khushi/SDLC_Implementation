"""Shared LangGraph workflow state (the "clipboard", DEVELOPER_GUIDE.md §5A).

Each agent receives this state, updates ONLY the fields it owns, and returns it. It carries
both the guide's linear-pipeline fields (``review_report`` … one per downstream agent) and the
Code Generation agent's IMP-001 fields (``work_items``, ``gate_result``, ``repair_attempt`` …).

Three LOCAL counters live here and must NOT be conflated with each other or with ``attempt``
(CLAUDE.md rule 3): ``repair_attempt`` (reset per work item), ``debug_attempt`` (reset once,
post-commit debug/test loop), and ``security_loop_attempt`` (reset once, the Security<->
Refactoring loop, capped by ``router.SECURITY_LOOP_CAP``). ``attempt`` is the ORCHESTRATOR's
number, echoed back unchanged (this service never increments it).
"""

from typing import Any, NotRequired, TypedDict

from app.models import WorkItem


class GateCheck(TypedDict):
    """Result of one fixed-path check (compile/build/test/lint) against the current work item."""

    name: str        # "compile" | "build" | "test" | "lint"
    passed: bool
    stderr: str       # captured stderr (empty when passed) — the gate/router reads this
    stdout: str       # captured stdout (empty when passed) — test runners (jest/vitest) commonly
                       # put the actual failing assertions here, not on stderr; the repair path
                       # (app/agents/debugging.py) needs both to see the real failure reason
    exit_code: int
    # Project-relative directory the failure is scoped to (mirrors Executor.CheckResult.scope);
    # "" when the check isn't directory-scoped. Lets the Debugging agent narrow which generated
    # files a fix prompt needs instead of every file in the whole run.
    scope: str
    # The check failed for an environment reason the code cannot cause (Executor.CheckResult):
    # it proved nothing, so routing treats it as "not verified", never as work for the Debugging
    # agent. Absent on checks recorded before this existed — read with .get().
    inconclusive: NotRequired[bool]
    environment: NotRequired[str]


class GateResult(TypedDict):
    """Gate outcome: overall pass + per-check pass/fail + captured stderr (router source)."""

    passed: bool                # overall: every check in `checks` passed
    checks: list[GateCheck]     # per-check breakdown
    # Not passed, but every failing check was inconclusive (environment) — the run continues,
    # marked unverified. Absent on results recorded before this existed.
    inconclusive: NotRequired[bool]


class WorkflowState(TypedDict, total=False):
    # --- Identity / run metadata ---
    project_id: str
    run_id: str          # this service's id for the run
    attempt: int         # orchestrator's attempt number; echoed unchanged, never incremented here

    # --- Input ---
    # The design pack: a bundle of named artifacts (openapi.yaml, schema.sql,
    # validation-rules.json, routes.json, tokens.json, mockup.html, SKILL.md, ...) keyed by
    # name. Values are the artifact content (str) or parsed structures. Schema = 27 inputs, TBD.
    design_package: dict[str, Any]
    # Pack preflight (app/services/pack_preflight.py), computed by scaffold_node: the pack's
    # contradictions and gaps (findings) and the canonical values chosen for them (facts — roles,
    # enums, env names, error envelope, symbol owners ...), plus the Markdown report. Never set by
    # new_state: an absent value means "no preflight ran" (brownfield never runs it).
    pack_findings: list[dict[str, Any]]
    pack_facts: dict[str, Any]
    pack_preflight_report: str
    pack_preflight_report_path: str

    # --- Brownfield input (an EXISTING codebase + a change request, instead of a design pack) ---
    # "greenfield" (default, and what an absent value means) builds a new app from design_package.
    # "brownfield" clones source_repo_url and implements change_request against it. Read ONLY by
    # router.route_entry — no node branches on it (CLAUDE.md rule 3: branching lives in the router).
    source_mode: str
    # The EXISTING repo to modify. Deliberately NOT ``repo_url``: that one is an OUTPUT, written by
    # the scaffold/commit push steps, and overloading it would make "did we create this repo or was
    # it handed to us?" unanswerable — the single most dangerous ambiguity in brownfield mode.
    source_repo_url: str
    # {id, title, kind, description, acceptance_criteria[], constraints[]}. A plain dict, not a
    # model: graph._build_checkpointer allow-lists WorkItem alone for msgpack, so any other pydantic
    # type on state silently deserialises to a bare dict after a checkpoint round-trip. Shape is
    # validated at the API boundary instead (api/request_models.py).
    change_request: dict[str, Any]
    base_ref: str          # branch/tag/sha to start from; "" = the repo's default branch

    # --- Brownfield internals (written by acquire_repo_node) ---
    base_sha: str                       # commit the clone was pinned at — the diff baseline
    baseline_digests: dict[str, str]    # project-relative path -> sha256 BEFORE any edit
    repo_inventory: dict[str, Any]      # RepoInventory.as_dict(): what the acquired repo contains
    change_plan_notes: str              # the planner's rationale / refusal reason, for the report
    change_plan_errors: list[str]       # why a proposed plan was rejected ([] = it was accepted)
    change_impacts: dict[str, list[str]]  # planned target -> files importing it (blast radius)
    changed_files: list[str]            # the change set this run actually wrote
    test_command: str                   # how this repo tests itself, and the evidence for it
    baseline_test: dict[str, Any]       # the suite's result BEFORE any edit
    verify_test: dict[str, Any]         # ...and after
    # passed | regressed | preexisting | fixed | unverified. 'unverified' is NOT a pass: it
    # means the suite could not run, so nothing was proven either way.
    verify_verdict: str
    modifier_notes: str                 # the editing agent's own account of what it changed
    change_report: str                  # Markdown record of the brownfield run
    change_report_path: str

    # --- Code Generation (IMP-001) internals ---
    work_items: list[WorkItem]            # design package decomposed into units of work
    work_item_index: int                  # graph cursor: index of the NEXT item to select
    current_work_item: WorkItem | None    # the item currently being generated
    generated_code: list[str]             # workspace-relative paths of files written this run
    scaffold_files: list[str]             # repo-root-relative boilerplate paths (for the main-branch commit)
    codegen_ok: bool                      # did the current item's generation succeed (files written)?
    gate_result: GateResult | None        # most recent gate evaluation (pass/fail + stderr)
    repair_attempt: int                   # LOCAL repair counter, reset per work item
    # LOCAL counter for the post-commit debug/test loop, reset once (not per work item like
    # repair_attempt above, and not the orchestrator's attempt either).
    #
    # PROGRESS-SENSITIVE (router.DEBUG_CAP): this counts CONSECUTIVE NO-PROGRESS rounds, not raw
    # entries into the debugging node. A round that actually reduced the failure count resets it
    # to 0. Rationale: the cap exists to stop a STUCK loop, not to bound total work — a large
    # generated project routinely has dozens of independent failures, and a flat per-entry cap
    # killed runs that were still converging (one observed run spent every one of its 10 attempts
    # on genuinely distinct problems and was cut off mid-progress, while attempts that fixed
    # nothing at all cost exactly the same as attempts that fixed three files).
    debug_attempt: int
    # The failure count the debug/test loop last observed, used to decide whether a round made
    # progress. -1 = "no round has been measured yet" (a real count can legitimately be 0).
    debug_last_failure_count: int
    # Which check ("compile/build" | "test" | "unknown") debug_last_failure_count was measured
    # from. A lower count only counts as progress when this round's check_name matches — comparing
    # counts ACROSS kinds compares different units (a compile failure is always reported as a bare
    # "1"), so a test suite regressing from "30 failing" to a broken build would otherwise read as
    # "1 < 30 -> progress" and wrongly reset the stall counter. "" = nothing measured yet.
    debug_last_failure_kind: str
    # TOTAL debugging rounds this run, regardless of progress — the absolute backstop against an
    # oscillating loop that keeps resetting debug_attempt forever (debugging.DEBUG_ROUNDS_CEILING).
    debug_rounds: int
    debug_result: GateResult | None        # most recent compile+build check outcome (Debugging phase fixed check)
    # Debugging: the accumulated Markdown report (one section per attempt) + where it was saved
    # (reports/…). Rewritten to disk on every attempt so a DEBUG_CAP escalation still leaves a
    # full record, not just the last attempt.
    debug_report: str
    debug_report_path: str
    tests_ok: bool                        # did unit-test generation produce at least one parseable test file?
    test_result: GateResult | None        # most recent `test` check outcome (Unit Test phase fixed check)
    unit_test_report: str          # Unit Test Generation: the Markdown report content
    unit_test_report_path: str     # Unit Test Generation: where the report .md was saved (reports/…)
    generation_summary: str               # human-readable free-text summary of the run
    generation_metrics: dict[str, Any]    # run-level metrics (generation-metrics.json shape)

    # --- Conversational code generation (chat-driven plan patching) ---
    # Master switch, copied from settings at run start. Falsy (the default, and what an absent value
    # means) makes chat_intake_node a pure pass-through, so a run is byte-identical to one from
    # before this feature existed.
    chat_enabled: bool
    # Consumed high-water mark: the highest chat_inbox seq this run has drained. MUST advance even
    # when a turn is refused or errors, or a message nobody can act on would be re-proposed by every
    # later turn and every later run.
    chat_seq: int
    # Messages drained this turn — the input to ChatPlannerAgent, written by chat_intake_node
    # immediately before it and cleared after. Plain dicts (see chat_log below).
    chat_pending: list[dict[str, Any]]
    # The planner's proposed PlanPatch, model_dump()'d. A dict rather than the pydantic model
    # because _build_checkpointer allow-lists WorkItem alone for msgpack — any other pydantic type
    # silently deserialises to a bare dict after a checkpoint round-trip, so it is stored as one.
    chat_patch: dict[str, Any]
    chat_planner_notes: str          # the planner's rationale, or its refusal reason, for the report
    chat_log: list[dict[str, Any]]   # one row per applied turn: {seq, text, report}
    # Applied turns this run, capped by router.CHAT_TURN_CAP. A LOCAL counter in the same family as
    # repair_attempt/debug_attempt — it is NOT the orchestrator's `attempt` and never touches it.
    chat_turns: int
    # What each work item ACTUALLY wrote: [{work_item_id, feature_id, requirement_ids, files,
    # action}]. Written by code_generator on success; read by plan_patch to decide whether a
    # "modify" request can retarget a pending item or needs a new item over real files. Observed
    # files, deliberately not the pack's predicted target_files — see requirement_ledger's docstring.
    requirement_ledger: list[dict[str, Any]]
    # Digests of the current item's target_files, taken by select_work_item_node when it selects an
    # action="modify" item and read by gate_node to prove the targets actually moved. files_complete
    # cannot answer that: the files already exist, so it passes on a no-op edit.
    pre_item_digests: dict[str, str]
    # Files left behind by a conversational "remove". Reported, never deleted — there is no
    # Executor.delete_file, and per-feature commits may already be pushed.
    orphan_files: list[str]

    # --- Git push (opt-in; mandatory workflow rules 4 & 8) ---
    # When push_enabled AND git_remote are set, the commit step pushes 'main' after the scaffold
    # commit and 'dev' immediately after EACH feature commit, stopping the run if a push fails
    # (the next feature commits only after the previous push succeeds). The fixed
    # refactoring_publish node reuses these SAME two flags to push one more 'dev' commit after
    # Refactoring, when it edited files — unlike the two pushes above, a failure there is only
    # logged/noted (generation_summary) and never stops the run, since the debug/test loop can
    # still verify the refactored code from the shared sandbox workspace. git_remote is a GitHub
    # "owner/name" slug OR any git remote URL/path; git_token (optional) authenticates the push.
    push_enabled: bool
    git_remote: str
    git_token: str

    # --- Code Review inputs (repo to clone) + git working model ---
    # The public GitHub URL of the generated project. Produced by Code Generation (from the push
    # step) and consumed by Code Review, which clones it into an ephemeral sandbox for analysis.
    repo_url: str
    # ``branch`` is the working branch cloned/reviewed (defaults to settings.working_branch);
    # ``commit_sha`` is the exact commit Code Review pinned for the audit trail.
    branch: str
    commit_sha: str
    # The branch ``finalize`` opens its PR AGAINST. Empty/absent means "main" — correct for a repo
    # this service scaffolded, since publish_scaffold creates main itself. It is a field rather than
    # a literal because a repo we did NOT create may use master/develop/trunk, and a PR against a
    # branch that does not exist fails at the very last step of a multi-hour run.
    base_branch: str

    # --- Downstream pipeline agent outputs (each agent writes only its own) ---
    review_report: str          # Code Review: the Markdown report content
    review_report_path: str     # Code Review: where the report .md was saved (reports/…)
    review_findings_path: str   # Code Review: normalized verified-findings JSON (for Refactoring)
    refactored_code: str
    # Refactoring: workspace-relative (project-prefixed) paths the agent EDITED this run — the
    # input to the fixed refactoring_publish node (commit + push to 'dev'); empty when nothing
    # was edited (clean review / early exit), which makes the publish step a no-op.
    refactored_files: list[str]
    refactoring_report: str        # Refactoring: the Markdown report content
    refactoring_report_path: str   # Refactoring: where the report .md was saved (reports/…)
    # Relative imports the deterministic reconcile pass could not resolve against the generated
    # file set (app/services/wiring.py::find_unresolved_imports), as plain dicts (see
    # UnresolvedImport.to_dict()) — not pre-rendered strings, so the Debugging agent can both
    # render them into its prompt AND prune an entry once a later round resolves it (see
    # app/agents/debugging.py::_prune_unresolved). Report-only — deliberately NOT auto-fixed,
    # because "create the missing module" vs "rename the importer" is a judgement call. Passed into
    # the Debugging agent's prompt so it starts from the full list instead of rediscovering them one
    # build error at a time.
    unresolved_imports: list[dict[str, Any]]
    # Cross-file disagreements the reconcile pass can report but not settle without picking a side
    # (app/services/wiring.py::find_integration_issues): routes not under the pack's API base path,
    # a frontend calling a base path the backend does not serve, a router nothing mounts, a
    # define-once helper defined twice, an async engine behind sync sessions. ``{kind, detail,
    # files}`` dicts, for the run summary and the Debugging agent.
    integration_findings: list[dict[str, Any]]
    # Checks that could not run to a verdict because of the ENVIRONMENT (a wheel needing a missing
    # compiler, an unreachable registry, no database, an executor that cannot start a server):
    # ``{check, reason}``, latest per check. The run carried on; this is what its report, README
    # and pull request must say was NOT verified.
    unverified_checks: list[dict[str, Any]]
    unit_tests: list[str]                  # workspace-relative paths of test files written
    unit_test_report: str        # Unit Test: the Markdown report content (per-work-item coverage)
    unit_test_report_path: str   # Unit Test: where the report .md was saved (reports/…)
    debugging_report: str        # Debugging: the Markdown report content (what each round changed)
    debugging_report_path: str   # Debugging: where the report .md was saved (reports/…)
    documentation: str
    security_report: str
    security_report_path: str   # Security: where the report .md was saved (reports/…)
    security_verdict: str       # Security: "approve" | "changes_requested" — routing signal
    security_findings_path: str # Security: normalized Semgrep findings JSON (for Refactoring)

    # --- Security <-> Refactoring loop + finalize (dev -> main) + packaging ---
    # LOCAL counter (like repair_attempt/debug_attempt): incremented by Refactoring only when
    # re-entered from this loop (never on its original, one-shot post-Code-Review call — see
    # RefactoringAgent.execute). Capped by router.SECURITY_LOOP_CAP.
    security_loop_attempt: int
    pr_url: str                  # finalize: URL of the created/updated dev->main PR
    finalize_status: str         # finalize: "pr_created" | "pr_failed" | "skipped"
    package_path: str            # package: local path of the zipped project + docs (the run's output)

    # --- Lifecycle ---
    workflow_status: str


def new_state(
    *,
    run_id: str,
    attempt: int,
    project_id: str = "",
    design_package: dict[str, Any] | None = None,
    work_items: list[WorkItem] | None = None,
    push_enabled: bool = False,
    git_remote: str = "",
    git_token: str = "",
    source_mode: str = "greenfield",
    source_repo_url: str = "",
    change_request: dict[str, Any] | None = None,
    base_ref: str = "",
    chat_enabled: bool = False,
) -> WorkflowState:
    """Build the initial state for a run.

    Identity + input + Code Generation internals get their starting values; downstream agents'
    output fields are left unset (each adds its own). ``repair_attempt`` starts at 0.

    Push is OFF by default (``push_enabled=False``): the commit step commits ``main``/``dev``
    locally but pushes nothing. Set ``push_enabled=True`` + ``git_remote`` (and optionally
    ``git_token``) to push ``main`` after the scaffold, ``dev`` after each feature, and ``dev``
    once more after Refactoring when it edited files (that last push is non-fatal on failure).

    Chat is OFF by default (``chat_enabled=False``): every conversational node is a pass-through, so
    every existing caller is byte-identical to before the feature existed. Set it to ``True`` (the
    scripts read ``settings.chat_enabled``) to let ``chat_intake`` drain the inbox between work items.

    Fails fast on a malformed ``work_items`` (must be a list) rather than crashing deep in the
    graph loop.
    """
    if work_items is not None and not isinstance(work_items, list):
        raise ValueError(f"work_items must be a list, got {type(work_items).__name__}")
    return {
        "project_id": project_id,
        "run_id": run_id,
        "attempt": attempt,
        "design_package": design_package or {},
        # Brownfield inputs. Defaulted so every existing (greenfield) caller is byte-identical:
        # route_entry sends anything that is not "brownfield" to the scaffold lane.
        "source_mode": source_mode,
        "source_repo_url": source_repo_url,
        "change_request": change_request or {},
        "base_ref": base_ref,
        "work_items": work_items or [],
        "work_item_index": 0,
        "current_work_item": None,
        "generated_code": [],
        "scaffold_files": [],
        "gate_result": None,
        "repair_attempt": 0,
        "debug_attempt": 0,
        "debug_last_failure_count": -1,  # -1 = nothing measured yet (0 is a legitimate count)
        "debug_last_failure_kind": "",
        "debug_rounds": 0,
        "debug_result": None,
        "generation_summary": "",
        "generation_metrics": {},
        "push_enabled": push_enabled,
        "git_remote": git_remote,
        "git_token": git_token,
        "security_loop_attempt": 0,
        # Conversational code generation. Defaulted so a caller that knows nothing about chat gets
        # exactly today's behaviour: chat_intake sees chat_enabled False and passes straight through.
        "chat_enabled": chat_enabled,
        "chat_seq": 0,
        "chat_pending": [],
        "chat_patch": {},
        "chat_planner_notes": "",
        "chat_log": [],
        "chat_turns": 0,
        "requirement_ledger": [],
        "pre_item_digests": {},
        "orphan_files": [],
        "workflow_status": "pending",
    }
