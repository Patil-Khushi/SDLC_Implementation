"""The agentic file-editing tool loop, shared by every agent that edits an existing codebase.

Two agents drive the same loop for different reasons: Refactoring applies the fixes a code review
named, and the Change Modifier implements a change request. Both hand the model a ``read_file`` /
``write_file`` pair scoped to one project directory and let it work like a coding agent. Keeping one
implementation means the write GUARDS below cannot end up protecting one caller and not the other.

``write_file`` is a whole-file overwrite, and the model's whole reply is bounded by
``settings.llm_max_tokens``. A file too big to carry back comes back cut off, and the truncated
result is written, recorded as edited, then committed and pushed — corruption that looks exactly
like a successful edit. :func:`rewrite_refusal` turns that silent case into an error string the
model receives and can act on.

Extracted verbatim from ``RefactoringAgent._editing_tools``; ``test_refactoring.py`` passing
unmodified is the proof the extraction was behaviour-preserving.
"""

from __future__ import annotations

import logging
from typing import Any

from app.integrations.executor import Executor, RepairTool

logger = logging.getLogger(__name__)

#: Largest existing file an agent will overwrite wholesale. Past roughly this size the reply cannot
#: physically carry the file back, so the write lands truncated — valid enough to commit, missing
#: the tail. Refusing loudly ("a human must edit this one") beats destroying the end of a file.
MAX_EDITABLE_FILE_CHARS = 60_000

#: A rewrite returning less than this fraction of the original is treated as truncation, not as an
#: intentional deletion. An edit-shaped change does not delete half a file, so the false-positive
#: risk is far smaller than the corruption it catches. Applies at EVERY size, which is what makes
#: it the real guard — the ceiling above only covers the extremes.
MIN_REWRITE_SIZE_RATIO = 0.5


def project_path(project_dir: str, path: str) -> str:
    """Map a model-proposed, project-relative path to its workspace path under ``project_dir``.
    Idempotent: a path the model already prefixed with ``project_dir/`` is not double-prefixed."""
    rel = path.lstrip("/")
    prefix = f"{project_dir}/"
    if rel.startswith(prefix):
        rel = rel[len(prefix):]
    return f"{project_dir}/{rel}"


def rewrite_refusal(path: str, before: str, after: str) -> str:
    """Why this whole-file overwrite must be refused, or ``""`` to allow it.

    Returned text is addressed TO THE MODEL — ``llm_gateway._run_tool`` feeds a handler's return
    value straight back as the tool result — so it says what to do instead, not just what is wrong.
    """
    if len(before) > MAX_EDITABLE_FILE_CHARS:
        return (
            f"REFUSED: {path} is {len(before):,} chars, above the {MAX_EDITABLE_FILE_CHARS:,}-char "
            "limit for a whole-file overwrite, so a rewrite would very likely be truncated and lose "
            "code. Do NOT retry this file. Leave it unchanged and note in your summary that it "
            "needs a manual edit."
        )
    floor = int(len(before) * MIN_REWRITE_SIZE_RATIO)
    if before and len(after) < floor:
        return (
            f"REFUSED: the new content for {path} is {len(after):,} chars but the current file is "
            f"{len(before):,} — that is a truncated reply, not an edit. Re-read the file and send "
            "back its COMPLETE text with only the intended fix applied. If you really do mean to "
            "delete most of the file, say so in your summary instead and leave it unchanged."
        )
    return ""


def editing_tools(
    executor: Executor,
    project_dir: str,
    touched: list[str],
    *,
    allowed_paths: set[str] | None = None,
    label: str = "editing",
) -> list[Any]:
    """The ``read_file`` / ``write_file`` pair, scoped to ``project_dir``.

    ``touched`` accumulates every workspace path actually written, so the caller knows what changed
    without asking the model to report it.

    ``allowed_paths`` (project-relative) confines writes to a declared set. Refactoring passes none
    — it edits whatever the review flagged. The Change Modifier passes its work item's targets,
    because a plan that was validated file-by-file is only meaningful if the edit stays inside it;
    without this the model could quietly rewrite a file no one reviewed. Reads are never restricted:
    understanding a change usually means reading its callers.
    """
    def _read(path: str) -> str:
        try:
            return executor.read_file(project_path(project_dir, path))
        except Exception as exc:  # noqa: BLE001 - report to the model, don't crash the loop
            return f"ERROR: could not read {path}: {type(exc).__name__}: {exc}"

    def _write(path: str, content: str) -> str:
        rel = path.lstrip("/")
        if rel.startswith(f"{project_dir}/"):
            rel = rel[len(project_dir) + 1:]
        if allowed_paths is not None and rel not in allowed_paths:
            logger.warning("[%s] refused out-of-plan write to %s", label, rel)
            return (
                f"REFUSED: {rel} is not one of this work item's target files "
                f"({', '.join(sorted(allowed_paths)) or 'none'}). Only those files were reviewed "
                "and approved for editing. Make the change within them, or explain in your summary "
                "why it cannot be done there."
            )

        out_path = project_path(project_dir, path)
        # Both guards need the CURRENT file. A file that cannot be read is simply a new one (or an
        # unreadable path the write itself will fail on), so "no baseline" never blocks a write.
        try:
            before = executor.read_file(out_path)
        except Exception:  # noqa: BLE001 - no baseline; treat as a create and let the write run
            before = None
        if before is not None:
            refusal = rewrite_refusal(path, before, content)
            if refusal:
                logger.warning("[%s] refused write to %s: %s", label, path, refusal)
                return refusal

        executor.write_file(out_path, content)
        if out_path not in touched:
            touched.append(out_path)
        return f"wrote {path} ({len(content)} chars)"

    return [
        RepairTool(
            name="read_file",
            description="Read a file's current text content. Path is repo-relative.",
            handler=_read,
            input_schema={"type": "object", "properties": {"path": {"type": "string"}},
                          "required": ["path"]},
        ),
        RepairTool(
            name="write_file",
            description=(
                "Save the corrected FULL content of a file (overwrites it). Path is repo-relative. "
                "Use this to apply each fix."
            ),
            handler=_write,
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        ),
    ]
