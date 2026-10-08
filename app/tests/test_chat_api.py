"""``POST /api/chat`` must be answerable while a run is holding everything else.

The feature's headline claim is that a user can ask for a change *while code is being generated*.
That is only true if the accept path shares nothing with the run: the graph_server serialises runs
behind ``_run_lock`` and the executor and LLM gateway are process-global singletons, so an endpoint
that touched any of them would block for the duration of a 3-6 hour build.

So the tests that matter here are the negative ones: no lock, no ``_last_run``, no waiting.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.services import chat_inbox

gs = pytest.importorskip("scripts.graph_server")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_inbox, "DEFAULT_ROOT", tmp_path)
    gs._last_run.clear()
    return TestClient(gs.app)


def test_a_message_is_accepted_with_202_and_a_seq(client):
    r = client.post("/api/chat", json={"project": "demo", "text": "add a logout endpoint"})
    assert r.status_code == 202
    body = r.json()
    assert body["seq"] == 1
    assert body["queued"] == 1
    assert body["project"] == "demo"


def test_seq_increments_across_messages(client):
    for expected in (1, 2, 3):
        r = client.post("/api/chat", json={"project": "demo", "text": f"msg {expected}"})
        assert r.json()["seq"] == expected


def test_the_message_reaches_the_inbox_on_disk(client):
    client.post("/api/chat", json={"project": "demo", "text": "drop the wishlist"})
    rows = chat_inbox.read_new("demo", 0)
    assert [r["text"] for r in rows] == ["drop the wishlist"]
    assert rows[0]["received_at"], "the server stamps the clock"


def test_accepting_a_message_does_not_take_the_run_lock(client):
    # The property the whole design rests on. Hold the lock as a run would, and the endpoint must
    # still answer immediately.
    assert gs._run_lock.acquire(blocking=False)
    try:
        r = client.post("/api/chat", json={"project": "demo", "text": "mid-run request"})
        assert r.status_code == 202
    finally:
        gs._run_lock.release()


def test_accepting_a_message_does_not_disturb_last_run(client):
    gs._last_run.update({"project": "other", "status": "running"})
    client.post("/api/chat", json={"project": "demo", "text": "hello"})
    assert gs._last_run["project"] == "other"
    assert gs._last_run["status"] == "running"


@pytest.mark.parametrize("payload", [
    {"project": "demo", "text": ""},
    {"project": "demo", "text": "   "},
    {"project": "", "text": "hello"},
])
def test_blank_input_is_rejected(client, payload):
    assert client.post("/api/chat", json=payload).status_code == 400


def test_a_project_name_cannot_escape_the_inbox_root(client, tmp_path):
    r = client.post("/api/chat", json={"project": "../../escape", "text": "hi"})
    assert r.status_code == 202
    assert chat_inbox.inbox_path("../../escape", tmp_path).parent.resolve() == tmp_path.resolve()


# --------------------------------------------------------------- the log endpoint

def test_the_log_reports_unavailable_rather_than_404(client):
    # Matches the artifact endpoints' convention so the UI renders a reason, not an error.
    body = client.get("/api/chat/log", params={"project": "nobody"}).json()
    assert body["available"] is False
    assert "nothing has been sent" in body["reason"]
    assert body["messages"] == [] and body["turns"] == []


def test_the_log_returns_accepted_messages(client):
    client.post("/api/chat", json={"project": "demo", "text": "one"})
    client.post("/api/chat", json={"project": "demo", "text": "two"})

    body = client.get("/api/chat/log", params={"project": "demo"}).json()

    assert body["available"] is True
    assert [m["text"] for m in body["messages"]] == ["one", "two"]
    assert body["consumed_seq"] == 0, "no run has drained them"
    assert body["turns"] == []


def test_the_log_surfaces_applied_turns_from_the_live_run(client):
    client.post("/api/chat", json={"project": "demo", "text": "drop the wishlist"})
    gs._last_run.update({
        "project": "demo",
        "state": {"chat_seq": 1, "chat_log": [{"seq": 1, "messages": ["drop the wishlist"],
                                               "report": {"removed": ["WI-003"]}}]},
    })

    body = client.get("/api/chat/log", params={"project": "demo"}).json()

    assert body["consumed_seq"] == 1
    assert body["turns"][0]["report"]["removed"] == ["WI-003"]


def test_queued_count_excludes_what_the_run_already_consumed(client):
    gs._last_run.update({"project": "demo", "state": {"chat_seq": 1}})
    client.post("/api/chat", json={"project": "demo", "text": "first"})
    r = client.post("/api/chat", json={"project": "demo", "text": "second"})
    # seq 1 is already consumed by the live run, so only seq 2 is outstanding.
    assert r.json()["queued"] == 1


def test_a_turn_log_for_a_different_project_is_not_leaked(client):
    client.post("/api/chat", json={"project": "demo", "text": "hello"})
    gs._last_run.update({"project": "other", "state": {"chat_seq": 9, "chat_log": [{"seq": 9}]}})

    body = client.get("/api/chat/log", params={"project": "demo"}).json()

    assert body["turns"] == []
    assert body["consumed_seq"] == 0
