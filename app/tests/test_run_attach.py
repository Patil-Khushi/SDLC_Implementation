"""Reattaching to a run in flight, and reading one back after it finished.

The bug this machinery exists for: a client whose SSE connection dropped could not get back to its
run. Re-opening ``/api/run/stream`` acquires ``_run_lock`` in order to START a run, and the original
driver thread is still holding it — a network blip in a browser does not stop a thread that is
hours into generating code — so the reconnect was answered "a run is already in progress" and gave
up, while the run carried on invisibly.

The fix separates readers from runs: one run at a time still, but any number of readers, joining
whenever they like. These tests pin the three properties that makes reliable:

* a reader that joins late gets every frame already emitted, THEN the live ones (no gap, no dupes),
* a reader still gets released when the run ends, rather than hanging on a dead queue, and
* the single-run guard is untouched — attaching is not a back door to starting a second run.
"""

from __future__ import annotations

import threading

import pytest

gs = pytest.importorskip("scripts.graph_server")


@pytest.fixture(autouse=True)
def fresh_bus():
    """Each test owns the module-level bus; leave it as found."""
    gs._bus.reset(project="", kind="")
    gs._bus.close()
    yield
    gs._bus.reset(project="", kind="")
    gs._bus.close()


def _frames(q, limit=100):
    """Drain a reader queue without blocking forever."""
    out = []
    while len(out) < limit:
        item = q.get(timeout=2)
        if item is gs._DONE:
            out.append("__DONE__")
            break
        out.append(item)
    return out


# --------------------------------------------------------------- fan-out

def test_every_subscriber_sees_every_frame():
    gs._bus.reset(project="p", kind="pack")
    a, b = gs._bus.subscribe(), gs._bus.subscribe()
    gs._bus.put("one")
    gs._bus.put("two")
    gs._bus.close()

    assert _frames(a) == ["one", "two", "__DONE__"]
    assert _frames(b) == ["one", "two", "__DONE__"]


def test_close_releases_readers_so_a_stream_never_hangs():
    gs._bus.reset(project="p", kind="pack")
    q = gs._bus.subscribe()
    gs._bus.close()
    assert q.get(timeout=2) is gs._DONE


def test_unsubscribe_stops_delivery_to_a_gone_client():
    gs._bus.reset(project="p", kind="pack")
    q = gs._bus.subscribe()
    gs._bus.unsubscribe(q)
    gs._bus.put("after-you-left")
    assert q.empty(), "a dropped reader's queue must not keep growing"


# --------------------------------------------------------------- attach

def test_a_late_reader_gets_the_backlog_then_the_live_tail():
    gs._bus.reset(project="p", kind="pack")
    gs._bus.put("before-1")
    gs._bus.put("before-2")

    status, kind, backlog, live = gs._bus.attach("p")
    assert status == "running"
    assert kind == "pack"
    assert backlog == ["before-1", "before-2"], "everything already emitted must be replayed"
    assert live is not None

    gs._bus.put("after-1")
    gs._bus.close()
    assert _frames(live) == ["after-1", "__DONE__"], "and only what came after, exactly once"


def test_attach_after_the_run_finished_replays_and_does_not_wait():
    # The browser-refresh case: nothing is running, but the last run is still worth showing.
    gs._bus.reset(project="p", kind="pack")
    gs._bus.put("frame-1")
    gs._bus.close()

    status, _kind, backlog, live = gs._bus.attach("p")
    assert status == "finished"
    assert backlog == ["frame-1"]
    assert live is None, "a finished run must not leave the client waiting on a live queue"


def test_attach_to_a_project_the_server_is_not_holding():
    gs._bus.reset(project="mine", kind="pack")
    status, _kind, backlog, live = gs._bus.attach("someone-elses")
    assert status == "none"
    assert backlog == []
    assert live is None


def test_an_empty_project_attaches_to_whatever_is_current():
    # How a client with no stored project asks "is anything running at all?".
    gs._bus.reset(project="current", kind="brownfield")
    gs._bus.put("f")
    status, kind, backlog, _live = gs._bus.attach("")
    assert status == "running"
    assert kind == "brownfield"
    assert backlog == ["f"]


def test_attach_is_atomic_against_a_concurrent_publish():
    """The snapshot and the subscribe happen under one lock, so a frame published in between is
    neither lost nor delivered twice. Hammer it to make a torn read visible if the lock is ever
    removed."""
    gs._bus.reset(project="p", kind="pack")
    stop = threading.Event()
    published: list[str] = []

    def publisher():
        i = 0
        while not stop.is_set() and i < 400:
            line = f"f{i}"
            gs._bus.put(line)
            published.append(line)
            i += 1

    t = threading.Thread(target=publisher)
    t.start()
    try:
        _status, _kind, backlog, live = gs._bus.attach("p")
    finally:
        stop.set()
        t.join()
    gs._bus.close()

    tail = [f for f in _frames(live, limit=1000) if f != "__DONE__"]
    seen = backlog + tail
    # No duplicates across the boundary, and the boundary is contiguous.
    assert len(seen) == len(set(seen)), "a frame was delivered twice across the attach boundary"
    assert seen == published[: len(seen)], "frames must arrive in order with no gap at the boundary"


# --------------------------------------------------------------- run identity

def test_reset_releases_the_previous_runs_readers():
    # Starting a new run must not leave a client from the old one blocked forever.
    gs._bus.reset(project="old", kind="pack")
    stale = gs._bus.subscribe()
    gs._bus.reset(project="new", kind="pack")
    assert stale.get(timeout=2) is gs._DONE
    assert gs._bus.project == "new"


def test_reset_drops_the_previous_runs_backlog():
    gs._bus.reset(project="old", kind="pack")
    gs._bus.put("old-frame")
    gs._bus.reset(project="new", kind="pack")
    _status, _kind, backlog, _live = gs._bus.attach("new")
    assert backlog == [], "a new run must not replay the previous run's frames"


def test_snapshot_meta_reports_size_and_liveness():
    gs._bus.reset(project="p", kind="pack")
    gs._bus.put("a")
    assert gs._bus.snapshot_meta() == (1, True)
    gs._bus.close()
    assert gs._bus.snapshot_meta() == (1, False)


def test_the_replay_buffer_is_capped_but_keeps_the_head():
    # run_start and plan live at the very front; a client that replays without them cannot rebuild
    # the board at all, so trimming must eat from after the head, never the head itself.
    gs._bus.reset(project="p", kind="pack")
    total = gs._MAX_REPLAY_FRAMES + 200
    for i in range(total):
        gs._bus.put(f"f{i}")

    _status, _kind, backlog, _live = gs._bus.attach("p")
    assert len(backlog) <= gs._MAX_REPLAY_FRAMES
    assert backlog[: gs._REPLAY_HEAD_KEEP] == [f"f{i}" for i in range(gs._REPLAY_HEAD_KEEP)]
    assert backlog[-1] == f"f{total - 1}", "the most recent frame must always survive"
