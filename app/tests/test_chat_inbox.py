"""The chat inbox must accept a message while the run that will answer it is busy.

Everything here exists to pin the two properties that make the inbox usable as an acceptance
channel: it never blocks or shares state with a run, and it cannot be poisoned. The second matters
more than it looks — a 3-6 hour run killed with Ctrl-C mid-append leaves a truncated line, and if
that stranded every later turn the feature would be broken by its own crash recovery.

``seq`` monotonicity is the load-bearing invariant: ``WorkflowState["chat_seq"]`` is a consumed
high-water mark, so a duplicated or reused seq means a message is either replayed forever or
silently skipped.
"""

from __future__ import annotations

import json
import threading

import pytest

from app.services import chat_inbox

_AT = "2026-09-04T12:00:00Z"


def test_seq_starts_at_one_and_increments(tmp_path):
    assert chat_inbox.append("proj", "add logout", received_at=_AT, root=tmp_path) == 1
    assert chat_inbox.append("proj", "drop wishlist", received_at=_AT, root=tmp_path) == 2
    assert chat_inbox.append("proj", "login via OTP", received_at=_AT, root=tmp_path) == 3


def test_read_new_is_relative_to_the_consumed_high_water_mark(tmp_path):
    for text in ("one", "two", "three"):
        chat_inbox.append("proj", text, received_at=_AT, root=tmp_path)

    assert [r["text"] for r in chat_inbox.read_new("proj", 0, root=tmp_path)] == ["one", "two", "three"]
    assert [r["text"] for r in chat_inbox.read_new("proj", 2, root=tmp_path)] == ["three"]
    assert chat_inbox.read_new("proj", 3, root=tmp_path) == []


def test_rows_carry_the_passed_in_timestamp_not_a_clock(tmp_path):
    # received_at is injected so replay and tests stay deterministic; the HTTP handler owns the clock.
    chat_inbox.append("proj", "hello", received_at="2020-01-01T00:00:00Z", root=tmp_path)
    (row,) = chat_inbox.read_new("proj", 0, root=tmp_path)
    assert row["received_at"] == "2020-01-01T00:00:00Z"
    assert row["project"] == "proj"


def test_a_truncated_trailing_line_is_skipped_and_does_not_break_the_next_append(tmp_path):
    # Simulates a writer killed mid-line: the queue must stay usable.
    chat_inbox.append("proj", "first", received_at=_AT, root=tmp_path)
    path = chat_inbox.inbox_path("proj", tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 2, "text": "trunc')      # no closing brace, no newline

    assert [r["text"] for r in chat_inbox.read_new("proj", 0, root=tmp_path)] == ["first"]

    # The next append must not reuse seq 1, or the high-water mark would skip a real message.
    assert chat_inbox.append("proj", "second", received_at=_AT, root=tmp_path) == 2
    assert [r["text"] for r in chat_inbox.read_new("proj", 1, root=tmp_path)] == ["second"]


def test_garbage_and_non_object_lines_are_skipped(tmp_path):
    path = chat_inbox.inbox_path("proj", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "not json at all\n"
        + json.dumps([1, 2, 3]) + "\n"                       # valid JSON, wrong shape
        + json.dumps({"text": "no seq"}) + "\n"              # dict without an integer seq
        + json.dumps({"seq": 7, "text": "good", "project": "proj", "received_at": _AT}) + "\n",
        encoding="utf-8",
    )
    assert [r["text"] for r in chat_inbox.read_new("proj", 0, root=tmp_path)] == ["good"]
    # seq continues past the highest surviving row, not past the line count.
    assert chat_inbox.append("proj", "next", received_at=_AT, root=tmp_path) == 8


def test_empty_text_is_refused_without_consuming_a_seq(tmp_path):
    with pytest.raises(ValueError):
        chat_inbox.append("proj", "   ", received_at=_AT, root=tmp_path)
    assert chat_inbox.append("proj", "real", received_at=_AT, root=tmp_path) == 1


@pytest.mark.parametrize(
    "project",
    ["../escape", "..", ".", "a/b", "a\\b", "/abs", "üni…code"],
)
def test_a_project_name_cannot_select_a_path_outside_the_root(tmp_path, project):
    chat_inbox.append(project, "hi", received_at=_AT, root=tmp_path)
    path = chat_inbox.inbox_path(project, tmp_path)
    assert path.parent.resolve() == tmp_path.resolve()
    assert path.is_file()
    # And the message is still readable back under the same key.
    assert [r["text"] for r in chat_inbox.read_new(project, 0, root=tmp_path)] == ["hi"]


def test_concurrent_appends_produce_a_contiguous_seq_range_with_no_losses(tmp_path):
    # The HTTP handler may be hit from several worker threads while a run holds everything else.
    seqs: list[int] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        s = chat_inbox.append("proj", f"msg-{n}", received_at=_AT, root=tmp_path)
        with lock:
            seqs.append(s)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(seqs) == list(range(1, 26)), "seq must be unique and contiguous"
    assert len(chat_inbox.read_new("proj", 0, root=tmp_path)) == 25, "no line may be lost"


def test_pending_count_matches_read_new(tmp_path):
    for text in ("a", "b", "c"):
        chat_inbox.append("proj", text, received_at=_AT, root=tmp_path)
    assert chat_inbox.pending_count("proj", 1, root=tmp_path) == 2


def test_reading_a_project_with_no_inbox_is_empty_not_an_error(tmp_path):
    assert chat_inbox.read_new("never-seen", 0, root=tmp_path) == []
    assert chat_inbox.pending_count("never-seen", 0, root=tmp_path) == 0


def test_high_water_is_the_run_start_seed(tmp_path):
    # A fresh run seeds chat_seq from this, so it only ever sees messages sent WHILE it runs.
    # Without it, a project whose inbox holds an earlier build's requests would have its whole
    # conversational history re-applied to the new build.
    assert chat_inbox.high_water("never-seen", root=tmp_path) == 0
    for text in ("old one", "old two"):
        chat_inbox.append("proj", text, received_at=_AT, root=tmp_path)
    assert chat_inbox.high_water("proj", root=tmp_path) == 2
    # seeded at 2, a run sees nothing until something new arrives
    assert chat_inbox.read_new("proj", 2, root=tmp_path) == []
    chat_inbox.append("proj", "sent during the run", received_at=_AT, root=tmp_path)
    assert [r["text"] for r in chat_inbox.read_new("proj", 2, root=tmp_path)] == [
        "sent during the run"
    ]


def test_high_water_ignores_malformed_lines(tmp_path):
    chat_inbox.append("proj", "good", received_at=_AT, root=tmp_path)
    path = chat_inbox.inbox_path("proj", tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("garbage\n")
    assert chat_inbox.high_water("proj", root=tmp_path) == 1
