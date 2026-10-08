"""Append-only inbox for chat-driven change requests — deterministic, no LLM, no state.

This is the *acceptance* half of conversational code generation, and it is deliberately the
dumbest component in the feature. Its entire reason for existing is that a user must be able to
say "drop the wishlist and make login use OTP" **at any instant** — including while work item 23
of 59 is blocked inside a multi-minute LLM call.

That requirement is what dictates the design. A run holds process-global resources: the executor
and LLM gateway are singletons (``set_executor`` / a gateway patch) and ``graph_server`` serialises
runs behind a ``_run_lock`` for exactly that reason. So acceptance must not touch *any* of it:
appending a message takes no lock the graph holds, writes no ``WorkflowState``, and never waits on
the run. Application happens later, at a work-item boundary, in ``chat_intake_node``. Separating
those two concerns is the whole trick — it is why this works without an ``interrupt()``, without
``update_state()``, and without the graph needing to be idle.

Two consequences of "the writer may be killed mid-write":

* A **partial or malformed trailing line is skipped, never fatal.** A crash during ``append`` (or a
  ``Ctrl-C`` during a 3-6 hour run) must not poison the queue for every later turn. Readers
  tolerate garbage; they do not repair it.
* ``seq`` is derived from what is already **on disk**, not from an in-memory counter, so it stays
  correct across a process restart.

The file lives under ``app/workspace/chat/`` and NOT inside the generated project directory,
because ``graph_server._drive`` does ``shutil.rmtree`` over ``generated-apps/<project>`` — including
its ``.git`` — before every real run. An inbox stored there would be destroyed by the next run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Where inboxes live. A sibling of the other run scratch dirs, and deliberately NOT under the
#: generated project (see the module docstring).
DEFAULT_ROOT = Path("app/workspace/chat")

#: Characters allowed in the on-disk filename derived from a project id. Anything else is replaced,
#: so a hostile or careless project name cannot select a path.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

#: One lock for every inbox in this process. Appends are short (one line + fsync) and rare compared
#: with anything else in a run, so a single lock costs nothing and removes a whole class of
#: interleaving bug. It does NOT protect against a second *process* appending concurrently — the
#: O_APPEND write does that at the OS level for lines this small.
_LOCK = threading.Lock()


def safe_project(project: str) -> str:
    """Reduce a project id to a filename that cannot escape the inbox root.

    Path separators, ``..`` and anything outside ``[A-Za-z0-9._-]`` are collapsed to ``_``. A name
    that reduces to nothing (or to a bare dot run) becomes ``_``, so there is always exactly one
    file per project and never a directory traversal.
    """
    cleaned = _UNSAFE.sub("_", (project or "").strip())
    cleaned = cleaned.strip(".")          # ".." / "." can never survive as the whole name
    return cleaned or "_"


def inbox_path(project: str, root: Path | None = None) -> Path:
    """Path of ``project``'s inbox file. Does not create anything."""
    return (root or DEFAULT_ROOT) / f"{safe_project(project)}.jsonl"


def _read_rows(path: Path) -> list[dict[str, Any]]:
    """Every well-formed row in ``path``, in file order.

    Unparseable lines — a truncated tail from a killed writer, or anything hand-edited — are
    skipped with a warning rather than raised. A poisoned queue would strand every later turn, and
    the cost of dropping one malformed line is that one message is lost, which is recoverable by
    asking again.
    """
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            logger.warning("chat inbox %s: skipping malformed line %d", path, lineno)
            continue
        if isinstance(row, dict) and isinstance(row.get("seq"), int):
            rows.append(row)
        else:
            logger.warning("chat inbox %s: skipping line %d (no integer seq)", path, lineno)
    return rows


def _needs_newline(path: Path) -> bool:
    """Does ``path`` exist with content that does not end in a newline?

    True only for a file left mid-line by a killed writer. Reads one byte, not the file.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size == 0:
        return False
    with path.open("rb") as fh:
        fh.seek(-1, os.SEEK_END)
        return fh.read(1) != b"\n"


def _next_seq(rows: list[dict[str, Any]]) -> int:
    """One past the highest ``seq`` on disk.

    Taken from the maximum rather than the row count so that a dropped malformed line does not
    cause a duplicate seq — the consumed high-water mark (``WorkflowState["chat_seq"]``) must stay
    strictly monotonic or a message would be replayed.
    """
    return max((int(r["seq"]) for r in rows), default=0) + 1


def append(project: str, text: str, *, received_at: str, root: Path | None = None) -> int:
    """Queue one message for ``project``; return its ``seq`` (1-based).

    ``received_at`` is passed in rather than read from the clock here so that callers which must be
    deterministic (tests, replay) stay deterministic. The HTTP handler is the natural place to stamp
    a real clock.

    Raises ``ValueError`` on empty text — an empty message has no meaning and would consume a seq.
    """
    if not (text or "").strip():
        raise ValueError("chat message text must not be empty")

    path = inbox_path(project, root)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        seq = _next_seq(_read_rows(path))
        row = {"seq": seq, "text": text, "project": project, "received_at": received_at}
        # Heal a missing final newline BEFORE appending. A writer killed mid-line leaves a partial
        # record with no terminator, and appending straight onto it would splice the new message
        # into the corpse — losing the good message too, not just the truncated one. Readers already
        # skip the fragment; this keeps the fragment from taking the next message down with it.
        prefix = "\n" if _needs_newline(path) else ""
        # O_APPEND + fsync: the message must survive the crash of the run it is aimed at, since the
        # whole point is to accept it while that run is busy.
        with path.open("a", encoding="utf-8") as fh:
            fh.write(prefix + json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    logger.info("chat inbox %s: queued seq=%d (%d chars)", project, seq, len(text))
    return seq


def read_new(project: str, after_seq: int, *, root: Path | None = None) -> list[dict[str, Any]]:
    """Messages for ``project`` with ``seq > after_seq``, in order.

    Read-only: draining is the caller's job, recorded by advancing
    ``WorkflowState["chat_seq"]``. Note that ``chat_intake_node`` advances it even when the planner
    *refuses* the request — otherwise a message nobody can act on would be re-proposed by every
    later turn and every later run.
    """
    return [r for r in _read_rows(inbox_path(project, root)) if int(r["seq"]) > int(after_seq)]


def pending_count(project: str, after_seq: int, *, root: Path | None = None) -> int:
    """How many messages are waiting. Cheap enough for a no-op check on every loop pass."""
    return len(read_new(project, after_seq, root=root))


def high_water(project: str, *, root: Path | None = None) -> int:
    """The highest ``seq`` currently on disk; ``0`` for a project with no inbox.

    Used by the run drivers to seed ``WorkflowState["chat_seq"]`` at the START of a fresh run, so a
    run only ever sees messages sent *while it was running*. Without that seeding, a project whose
    inbox already holds requests from an earlier build would have its whole conversational history
    re-applied to the new one the first time ``chat_intake`` fires — a months-old "drop the
    wishlist" silently patching a fresh plan. ``--resume`` restores the checkpointed value instead
    and so is unaffected.
    """
    return max((int(r["seq"]) for r in _read_rows(inbox_path(project, root))), default=0)
