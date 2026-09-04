"""Validate a change plan before anything acts on it — deterministic, no LLM.

The change planner is an LLM deciding which files a prose change request should touch. Everything
downstream of it edits real files in someone else's repository, so its output is treated as a
PROPOSAL and checked here first. This is the same division the rest of the service already uses:
the model proposes, fixed code disposes (CLAUDE.md rules 2-3).

What it enforces, and why each one is not merely a nicety:

* **Paths exist / do not exist, as the action claims.** ``modify`` on a path that is not in the
  repo means the planner hallucinated a file, and the editing agent would then CREATE it — quietly
  adding a stray file instead of making the requested change. ``create`` on a path that already
  exists is the reverse: a whole-file overwrite of code nobody asked to replace.
* **No path escapes the project.** ``../`` or an absolute path reaches outside the clone.
* **Scope caps.** v1 is deliberately limited to localized changes; a plan naming half the repo is
  refused rather than attempted, because the service cannot verify a change of that size (there is
  no call-graph analysis, so a broad edit's effect on callers is unknown).
* **Forbidden paths.** CI workflow files, secrets, infrastructure state, lockfiles and .gitignore
  are refused outright. Two of these are load-bearing rather than tidy: a ``.github/workflows/``
  edit can exfiltrate repository secrets once merged, and replacing ``.gitignore`` un-ignores
  whatever it was protecting, which a later ``git add`` would then commit.
* **Disjoint targets.** Two items editing the same file fight: the second overwrites the first, and
  the per-item change gate cannot attribute the result to either.
* **No deletions in v1.** Deleting a file whose callers are unknown is unrecoverable from the
  pipeline's side, and the impact analysis that would justify it does not exist.

Returns errors rather than raising: a rejected plan is a routing decision (escalate with a stated
reason), not an exception, and every problem should be reported at once so a human sees the whole
picture instead of fixing them one run at a time.
"""

from __future__ import annotations

import posixpath
import re

from app.models import WorkItem

#: Largest number of files one change request may touch in v1. The agreed scope is localized
#: changes; beyond this the service is proposing an edit whose effect it cannot check.
MAX_FILES_PER_CHANGE = 5

#: Largest number of work items a plan may contain. A plan is one change request decomposed by
#: module, so more items than files is always a planner error.
MAX_ITEMS_PER_CHANGE = 5

#: Paths never edited by an automated change, whatever the request says. Matched against the
#: project-relative POSIX path.
FORBIDDEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\.git(/|$)"), "git internals"),
    (re.compile(r"^\.github/workflows/"), "CI workflow (a merged edit here can read repo secrets)"),
    (re.compile(r"(^|/)\.gitignore$"), "ignore rules (replacing them un-ignores protected files)"),
    (re.compile(r"(^|/)\.env"), "environment/secret file"),
    (re.compile(r"(^|/)secrets?(/|$)"), "secrets directory"),
    (re.compile(r"\.(pem|key|p12|pfx|keystore)$"), "private key material"),
    (re.compile(r"\.tf(state|vars)?$"), "infrastructure definition/state"),
    (re.compile(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock)$"),
     "dependency lockfile (regenerate it, never hand-edit)"),
    (re.compile(r"(^|/)(Dockerfile|docker-compose\.ya?ml)$"), "container build/runtime definition"),
)

#: Extensions a text-editing agent cannot meaningfully rewrite.
BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip", ".gz", ".tar", ".jar",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".so", ".dll", ".dylib", ".pyc", ".class",
    ".docx", ".xlsx", ".sqlite", ".db",
})


def _forbidden_reason(path: str) -> str:
    for pattern, reason in FORBIDDEN_PATTERNS:
        if pattern.search(path):
            return reason
    if posixpath.splitext(path)[1].lower() in BINARY_EXTENSIONS:
        return "binary file"
    return ""


def normalize_target(raw: str) -> str:
    """One canonical project-relative form for a path the planner proposed.

    EVERY check must run on this value, not on the raw string. ``_forbidden_reason``'s patterns are
    anchored (``^\\.git(/|$)``, ``^\\.github/workflows/``) and the existence check is a set
    membership test, so any prefix that collapses away — a leading ``./``, an inserted ``x/..``, a
    doubled slash — defeats both at once: ``src/../.github/workflows/ci.yml`` reaches the filesystem
    as ``.github/workflows/ci.yml`` while matching neither pattern, and ``./src/app.py`` declared as
    ``create`` is not found in the known-files set, so the "this would overwrite existing code"
    rule never fires. Normalizing in one place is what makes the rules below mean what they say.
    """
    return posixpath.normpath(raw.replace("\\", "/").strip())


def _escapes_project(path: str) -> bool:
    """True if ``path`` is absolute, or normalizes to somewhere outside the project root."""
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return True
    normalized = normalize_target(path)
    return normalized.startswith("..") or normalized in (".", "")


def validate_plan(
    items: list[WorkItem], repo_files: "set[str] | list[str]"
) -> tuple[list[WorkItem], list[str]]:
    """Check a proposed plan against the repository it will be applied to.

    ``repo_files`` is the acquired inventory's file list (project-relative). Returns the items
    unchanged plus every problem found; a non-empty error list means the plan must NOT be applied.
    The items come back so a caller can keep them for the report even when rejecting them — a
    rejected plan is the most useful thing to show a human.
    """
    # Both sides of every comparison below are normalized, so "does this path exist?" cannot be
    # answered differently for two spellings of the same file.
    known = {normalize_target(p) for p in repo_files}
    errors: list[str] = []

    if not items:
        return items, ["the planner produced no work items"]

    if len(items) > MAX_ITEMS_PER_CHANGE:
        errors.append(
            f"plan has {len(items)} work items, more than the {MAX_ITEMS_PER_CHANGE} allowed for a "
            "single change request - the request is too broad for automated implementation"
        )

    seen_ids: set[str] = set()
    owner_of: dict[str, str] = {}          # path -> the item id that already claims it
    total_targets = 0

    for item in items:
        if item.id in seen_ids:
            errors.append(f"duplicate work-item id '{item.id}'")
        seen_ids.add(item.id)

        if not item.target_files:
            errors.append(f"'{item.id}': no target_files - a work item that changes nothing")
            continue

        if item.action == "delete":
            errors.append(
                f"'{item.id}': action 'delete' is not supported - removing a file whose callers "
                "are unknown cannot be verified by this pipeline"
            )

        if not item.change_intent.strip():
            errors.append(
                f"'{item.id}': no change_intent - the editing agent would have no instruction "
                "beyond the file list"
            )

        for raw in item.target_files:
            total_targets += 1
            # Escape check FIRST (it must see the raw string to spot an absolute path), then
            # everything after it runs on the canonical form — see normalize_target.
            if _escapes_project(raw):
                errors.append(f"'{item.id}': target '{raw}' is outside the project")
                continue
            path = normalize_target(raw)

            reason = _forbidden_reason(path)
            if reason:
                errors.append(f"'{item.id}': target '{path}' is not editable - {reason}")
                continue

            if path in owner_of and owner_of[path] != item.id:
                errors.append(
                    f"target '{path}' is claimed by both '{owner_of[path]}' and '{item.id}' - "
                    "two items editing one file overwrite each other"
                )
            owner_of[path] = item.id

            exists = path in known
            if item.action == "modify" and not exists:
                errors.append(
                    f"'{item.id}': action 'modify' but '{path}' is not in the repository - "
                    "it would be created instead of changed"
                )
            elif item.action == "create" and exists:
                errors.append(
                    f"'{item.id}': action 'create' but '{path}' already exists - "
                    "it would be overwritten wholesale"
                )

    if total_targets > MAX_FILES_PER_CHANGE:
        errors.append(
            f"plan touches {total_targets} files, more than the {MAX_FILES_PER_CHANGE} allowed for "
            "a single change request"
        )

    return items, errors


def render_plan(items: list[WorkItem], errors: list[str], dependents: dict[str, list[str]]) -> str:
    """Markdown for the change report: what the planner proposed, what is wrong with it, and what
    else imports the files it wants to touch.

    ``dependents`` maps a target path to the files importing it — the blast radius. It is reported
    rather than acted on: this pipeline has no call-graph analysis, so the honest thing is to put
    the list in front of whoever reviews the eventual pull request.
    """
    lines = ["## Proposed plan", ""]
    if not items:
        lines.append("_The planner produced no work items._")
    for item in items:
        lines.append(f"### `{item.id}` - {item.action}")
        lines.append("")
        if item.change_intent:
            lines.append(item.change_intent)
            lines.append("")
        lines.append("| Target | Action | Imported by |")
        lines.append("| --- | --- | --- |")
        for raw in item.target_files:
            path = normalize_target(raw)
            # Look up under the canonical form — the producer keys on it too. A raw-vs-normalized
            # mismatch here would silently render "-", i.e. an affirmative "nothing imports this",
            # for a file whose dependents were computed perfectly well.
            impacted = dependents.get(path)
            if impacted is None:
                shown = "_(not analysed)_"      # never claim "nothing imports this" without looking
            elif not impacted:
                shown = "-"
            else:
                shown = ", ".join(f"`{d}`" for d in impacted[:5])
                if len(impacted) > 5:
                    shown += f" _(+{len(impacted) - 5} more)_"
            lines.append(f"| `{path}` | {item.action} | {shown} |")
        lines.append("")
        if item.acceptance_criteria:
            lines.append("**Acceptance criteria**")
            lines += [f"- {c}" for c in item.acceptance_criteria]
            lines.append("")

    if errors:
        lines += ["## Plan REJECTED", "",
                  "The plan was not applied. Every problem found:", ""]
        lines += [f"- {e}" for e in errors]
        lines.append("")
    return "\n".join(lines)
