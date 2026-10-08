"""The chat endpoint routing a message to the right intent.

``ProjectQAAgent`` is unit-tested separately; what belongs here is the endpoint's own decisions:
which reply shape each intent produces, that a change request actually reaches the inbox and a
question does NOT, and that a classification failure loses nothing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.project_qa import QAResult

gs = pytest.importorskip("scripts.graph_server")


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Point the inbox at a temp dir so a test never writes into the real workspace.
    monkeypatch.setattr(gs.chat_inbox, "DEFAULT_ROOT", tmp_path)
    gs._last_run.clear()
    return TestClient(gs.app)


def _stub_qa(monkeypatch, result: QAResult):
    """Replace the agent's ask() so no model call happens."""
    monkeypatch.setattr(gs.ProjectQAAgent, "ask", lambda self, q, **kw: result)


def test_a_question_is_answered_and_never_queued(client, monkeypatch, tmp_path):
    _stub_qa(monkeypatch, QAResult(kind="question", answer="17 remain."))

    r = client.post("/api/chat/message", json={"project": "p", "text": "how many left?"})

    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "answer"
    assert body["answer"] == "17 remain."
    assert "seq" not in body, "a question consumes no inbox sequence"
    assert gs.chat_inbox.read_new("p", 0, root=tmp_path) == [], "nothing may be queued"


def test_a_change_request_is_queued(client, monkeypatch, tmp_path):
    _stub_qa(monkeypatch, QAResult(kind="change", answer=""))

    r = client.post("/api/chat/message", json={"project": "p", "text": "drop the wishlist"})

    body = r.json()
    assert body["kind"] == "change"
    assert body["seq"] == 1
    queued = gs.chat_inbox.read_new("p", 0, root=tmp_path)
    assert [m["text"] for m in queued] == ["drop the wishlist"]


def test_a_mixed_message_queues_and_still_returns_the_answer(client, monkeypatch):
    _stub_qa(monkeypatch, QAResult(kind="change", answer="It fails because the OTP field is absent."))
    body = client.post("/api/chat/message", json={"project": "p", "text": "why broken, fix it"}).json()
    assert body["kind"] == "change"
    assert "OTP" in body["answer"]


def test_a_classification_failure_queues_rather_than_losing_the_message(client, monkeypatch, tmp_path):
    # Falling back to the pre-existing behaviour (queue it) keeps the message recoverable; dropping
    # it because the classifier was unreachable would be the worst outcome.
    _stub_qa(monkeypatch, QAResult(error="the assistant could not be reached (ConnectionError)"))

    body = client.post("/api/chat/message", json={"project": "p", "text": "anything"}).json()

    assert body["kind"] == "change"
    assert body["seq"] == 1
    assert "could not classify" in body["note"].lower()
    assert len(gs.chat_inbox.read_new("p", 0, root=tmp_path)) == 1


def test_it_reports_when_the_question_had_no_run_behind_it(client, monkeypatch):
    _stub_qa(monkeypatch, QAResult(kind="question", answer="No run yet."))
    body = client.post("/api/chat/message", json={"project": "unknown", "text": "status?"}).json()
    assert body["grounded"] is False
    assert "unknown" in body["note"]


def test_facts_are_only_used_for_the_matching_project(client, monkeypatch):
    # Answering a question about project B from project A's facts would be confidently wrong.
    gs._last_run.update({"project": "loaded", "state": {"generated_code": ["a.js"]}})
    seen: dict[str, object] = {}

    def capture(self, q, **kw):
        seen.update(kw)
        return QAResult(kind="question", answer="ok")

    monkeypatch.setattr(gs.ProjectQAAgent, "ask", capture)

    client.post("/api/chat/message", json={"project": "other", "text": "status?"})
    assert seen["facts"] == "", "a different project's facts must not be used"

    seen.clear()
    client.post("/api/chat/message", json={"project": "loaded", "text": "status?"})
    assert seen["facts"], "the loaded project's facts must be passed"


@pytest.mark.parametrize("payload", [
    {"project": "p", "text": "  "},
    {"project": "", "text": "hello"},
])
def test_blank_input_is_rejected(client, payload):
    assert client.post("/api/chat/message", json=payload).status_code == 400
