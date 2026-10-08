"""sha256 proof that a modify work item actually changed its targets — deterministic, no LLM.

The code-generation gate runs ``files_complete`` and nothing else: it verifies from disk that every
one of the current work item's ``target_files`` exists. For a work item that *creates* files that is
exactly the right check, and it is authoritative precisely because it reads disk rather than trusting
the generator's own report.

For a work item whose ``action`` is ``"modify"`` it is **vacuous**. The target already exists, so
``files_complete`` passes whether the agent rewrote the file, appended a comment, or did nothing at
all. Without the check in this module, a no-op modify sails through the gate, gets committed by
``feature_publish``, and reports success for a change that never happened — the worst failure mode
available to a conversational loop, because the user is told their request was applied.

So: snapshot the targets when the item is selected, re-snapshot at the gate, and require that at
least one target moved.

Deliberately NOT done here: hashing the whole project tree to detect collateral edits. Writes on the
modify path are fenced at the tool layer by ``editing_tools(allowed_paths=item.target_files)``, which
refuses a write outside the set before it reaches disk, so a whole-tree scan would add cost
proportional to project size for no additional safety. (This differs from a lane that edits a
codebase it did not write, where the fence is not the only concern.)

Every read goes through ``Executor`` so this behaves identically under ``FakeExecutor``,
``LocalDiskExecutor`` and ``MCPExecutor`` — there is no direct filesystem access, and no git.
"""

from __future__ import annotations

import hashlib
import logging

from app.agents.editing import project_path
from app.integrations.executor import Executor

logger = logging.getLogger(__name__)


def digest_of(text: str) -> str:
    """sha256 of ``text`` as UTF-8. Content-only — no path, size or mtime in the hash."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot(executor: Executor, project_dir: str, rel_paths: list[str] | None) -> dict[str, str]:
    """Map project-relative path -> digest for the paths that can be read right now.

    A path that cannot be read is **absent from the result** rather than raising or recording a
    sentinel. That is the normal state for a target which does not exist yet, and conflating
    "missing" with "empty" would let a file created during the item read as unchanged.

    Keys are project-relative (never workspace-absolute) so a snapshot stays comparable across
    executors and across a checkpoint round-trip.
    """
    out: dict[str, str] = {}
    for rel in rel_paths or []:
        if not rel:
            continue
        try:
            out[rel] = digest_of(executor.read_file(project_path(project_dir, rel)))
        except Exception as exc:  # noqa: BLE001 - any executor may raise its own read error type
            logger.debug("digest snapshot: %s unreadable (%s)", rel, exc)
    return out


def changed_paths(before: dict[str, str] | None, after: dict[str, str] | None) -> list[str]:
    """Paths whose content differs between the two snapshots, sorted.

    A path present in ``after`` but absent from ``before`` counts as changed: it was created during
    the item, which is a real modification of the tree.
    """
    before = before or {}
    after = after or {}
    return sorted(p for p, d in after.items() if before.get(p) != d)


def unchanged_paths(before: dict[str, str] | None, after: dict[str, str] | None) -> list[str]:
    """Paths present in both snapshots with identical content, sorted."""
    before = before or {}
    after = after or {}
    return sorted(p for p, d in after.items() if p in before and before[p] == d)


def any_target_changed(before: dict[str, str] | None, after: dict[str, str] | None) -> bool:
    """Did at least one target move? The gate's actual question for a modify item."""
    return bool(changed_paths(before, after))
