"""A compact, factual digest of a run — deterministic, no LLM, no I/O.

Built so a question about the project can be answered from what the run ACTUALLY did, rather than
from the model's guesses. Everything here is read straight out of ``WorkflowState``; nothing is
inferred, and nothing is invented.

Two constraints shape it:

**It must be small.** ``WorkflowState`` at the end of a real run carries hundreds of generated
paths, a 60-item plan, several full Markdown reports and a design pack — far more than belongs in a
prompt. So each section is summarized and capped, with counts kept exact even where the listings are
truncated ("333 files" is the useful fact; the 333rd path is not).

**It must be honest about absence.** A field that is empty because the phase has not run yet is
different from one that is empty because there was nothing to report, and the digest says which.
That is what lets the answering agent say "the security scan has not run yet" instead of "there are
no security findings" — the second is a claim the run never made.
"""

from __future__ import annotations

from typing import Any

#: Longest listing rendered for any one section. Counts stay exact; only the enumeration is cut.
_MAX_LIST = 40
#: Longest free-text field (the generation summary runs to tens of KB on a real run).
_MAX_TEXT = 3_000


def _field(obj: Any, name: str, default: str = "") -> Any:
    """Read ``name`` off a pydantic model OR a plain dict.

    ``work_items`` holds ``WorkItem`` models during a run but plain dicts after a checkpoint
    round-trip (the checkpointer's msgpack allow-list is why), so both shapes reach this module.
    A single accessor rather than an ``or`` chain across the two: ``getattr(x, n, None) or x.get(n)``
    looks equivalent but falls through to ``.get`` whenever the attribute is present and *falsy* —
    an empty ``feature_title`` is entirely normal, and on a model that fallthrough is an
    AttributeError, which is exactly how this failed the first time it met a real run.
    """
    if isinstance(obj, dict):
        value = obj.get(name)
    else:
        value = getattr(obj, name, None)
    return default if value is None else value


def _cap_list(values: list[Any] | None, limit: int = _MAX_LIST) -> tuple[list[str], int]:
    """``(shown, total)`` — the listing to render and the true count behind it."""
    items = [str(v) for v in (values or []) if v]
    return items[:limit], len(items)


def _lines(label: str, values: list[Any] | None, limit: int = _MAX_LIST) -> list[str]:
    shown, total = _cap_list(values, limit)
    if total == 0:
        return []
    out = [f"{label} ({total}):"]
    out += [f"  - {v}" for v in shown]
    if total > len(shown):
        out.append(f"  ... and {total - len(shown)} more")
    return out


def build_facts(state: dict[str, Any] | None, *, meta: dict[str, Any] | None = None) -> str:
    """Render the run's facts as plain text for a prompt.

    ``state`` is a ``WorkflowState`` (or the snapshot of one kept after a run). ``meta`` is the
    server's own bookkeeping — project name, mode, status, timestamps — which lives outside the
    graph state.
    """
    state = state or {}
    meta = meta or {}
    out: list[str] = []

    # --- identity -------------------------------------------------------------------------------
    out.append("## Run")
    out.append(f"project: {meta.get('project') or state.get('project_id') or '(unknown)'}")
    if meta.get("mode"):
        out.append(f"mode: {meta['mode']}")
    if meta.get("pack"):
        out.append(f"design pack: {meta['pack']}")
    status = meta.get("status") or state.get("workflow_status") or "(unknown)"
    out.append(f"status: {status}")
    if meta.get("started_at"):
        out.append(f"started: {meta['started_at']}")
    if meta.get("finished_at"):
        out.append(f"finished: {meta['finished_at']}")
    if state.get("source_mode") == "brownfield":
        out.append(f"mode: brownfield, against {state.get('source_repo_url') or '(unknown repo)'}")

    # --- progress -------------------------------------------------------------------------------
    items = state.get("work_items") or []
    cursor = int(state.get("work_item_index", 0) or 0)
    out.append("")
    out.append("## Progress")
    if items:
        out.append(f"work items: {len(items)} total, {min(cursor, len(items))} selected so far")
        remaining = max(0, len(items) - cursor)
        out.append(f"remaining (not yet started): {remaining}")
        current = state.get("current_work_item")
        if current is not None:
            out.append(f"current work item: {_field(current, 'id', '?')}")
    else:
        out.append("work items: none (the plan has not been built, or this is a brownfield run)")

    # --- the plan itself ------------------------------------------------------------------------
    if items:
        rendered = []
        for idx, it in enumerate(items[:_MAX_LIST]):
            iid = _field(it, "id", "?") or "?"
            title = _field(it, "feature_title")
            action = _field(it, "action", "create") or "create"
            done = "done" if idx < cursor else "pending"
            rendered.append(f"{iid} [{action}, {done}]{f' — {title}' if title else ''}")
        out.append("")
        out += _lines("## Plan", rendered)

    # --- what was produced ----------------------------------------------------------------------
    generated = state.get("generated_code") or []
    tests = state.get("unit_tests") or []
    out.append("")
    out.append("## Output")
    out.append(f"source files written: {len(generated)}")
    out.append(f"test files written: {len(tests)}")
    if generated:
        out += _lines("sample of generated files", generated, limit=25)
    for key, label in (
        ("repo_url", "repository"),
        ("pr_url", "pull request"),
        ("branch", "working branch"),
        ("base_branch", "base branch"),
        ("package_path", "output zip"),
    ):
        if state.get(key):
            out.append(f"{label}: {state[key]}")

    # --- traceability ---------------------------------------------------------------------------
    ledger = state.get("requirement_ledger") or []
    if ledger:
        rows = []
        for row in ledger[:_MAX_LIST]:
            reqs = ", ".join(row.get("requirement_ids") or []) or "-"
            files = row.get("files") or []
            rows.append(f"{row.get('work_item_id')}: reqs={reqs}, {len(files)} file(s): "
                        f"{', '.join(files[:6])}{' ...' if len(files) > 6 else ''}")
        out.append("")
        out += _lines("## What each work item wrote", rows)

    # --- reports (presence is the fact; the content lives on disk) -------------------------------
    out.append("")
    out.append("## Reports")
    any_report = False
    for key, label in (
        ("review_report_path", "code review"),
        ("refactoring_report_path", "refactoring"),
        ("security_report_path", "security"),
        ("debugging_report_path", "debugging"),
        ("unit_test_report_path", "unit tests"),
        ("change_report_path", "change request"),
    ):
        if state.get(key):
            out.append(f"{label}: {state[key]}")
            any_report = True
    if not any_report:
        out.append("none written yet — the phases that produce them have not finished")

    # --- phase-specific outcomes ----------------------------------------------------------------
    verdict = state.get("security_verdict")
    if verdict:
        out.append("")
        out.append(f"## Security\nverdict: {verdict}")
    elif state.get("security_report_path"):
        out.append("")
        out.append("## Security\nthe scan ran; see the report path above")

    if state.get("refactored_files"):
        shown, total = _cap_list(state.get("refactored_files"), 20)
        out.append("")
        out.append(f"## Refactoring\nedited {total} file(s): {', '.join(shown)}")

    if state.get("verify_verdict"):
        out.append("")
        out.append(f"## Change verification\nverdict: {state['verify_verdict']} "
                   f"(test command: {state.get('test_command') or 'not detected'})")

    if state.get("orphan_files"):
        shown, total = _cap_list(state.get("orphan_files"), 20)
        out.append("")
        out.append(f"## Orphaned by a removal\n{total} file(s) left unwired (not deleted): "
                   f"{', '.join(shown)}")

    unresolved = state.get("unresolved_imports") or []
    if unresolved:
        out.append("")
        out.append(f"## Known problems\n{len(unresolved)} unresolved import(s) reported by the "
                   "deterministic reconcile pass (report-only, not auto-fixed)")

    # --- conversation ---------------------------------------------------------------------------
    chat_log = state.get("chat_log") or []
    if chat_log:
        rows = []
        for row in chat_log[-_MAX_LIST:]:
            msgs = " | ".join(row.get("messages") or [])
            rep = row.get("report") or {}
            if rep.get("refusal"):
                rows.append(f"#{row.get('seq')}: {msgs} -> declined: {rep['refusal']}")
            else:
                rows.append(
                    f"#{row.get('seq')}: {msgs} -> +{len(rep.get('added') or [])} "
                    f"-{len(rep.get('removed') or [])} ~{len(rep.get('modified') or [])}"
                )
        out.append("")
        out += _lines("## Change requests applied this run", rows)

    # --- the run's own narrative ----------------------------------------------------------------
    summary = str(state.get("generation_summary") or "").strip()
    if summary:
        out.append("")
        out.append("## Run log (the pipeline's own per-item summary)")
        out.append(summary[-_MAX_TEXT:] if len(summary) > _MAX_TEXT else summary)

    metrics = state.get("generation_metrics") or {}
    if metrics:
        out.append("")
        out.append("## Metrics")
        for k, v in list(metrics.items())[:20]:
            if isinstance(v, (str, int, float, bool)):
                out.append(f"{k}: {v}")

    return "\n".join(out)


def has_run(state: dict[str, Any] | None, meta: dict[str, Any] | None = None) -> bool:
    """Is there any real run to answer questions about?

    Distinguishes "nothing has run" (say so plainly) from "a run exists but a given phase has not
    finished" (which the digest itself explains).
    """
    state = state or {}
    meta = meta or {}
    return bool(
        meta.get("project")
        or state.get("project_id")
        or state.get("work_items")
        or state.get("generated_code")
    )
