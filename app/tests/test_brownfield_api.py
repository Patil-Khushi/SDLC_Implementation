"""The brownfield bridge's own layer: validation and the submit/stream token handoff.

Everything that does NOT require a real clone or a real LLM call — the full run itself is already
exercised manually (a real clone of a public repo, streamed to completion) and is graph/agent logic
this module does not own. What belongs here is the bridge's own contract: bad input is rejected
before a token is even issued, a token is claimed by exactly one stream, and a claimed token cannot
be replayed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.integrations.executor import RunResult

gs = pytest.importorskip("scripts.graph_server")
from scripts.local_executor import LocalDiskExecutor  # noqa: E402


@pytest.fixture
def client():
    gs._pending_change_requests.clear()
    return TestClient(gs.app)


def test_a_valid_submission_is_accepted_and_queued(client):
    r = client.post("/api/brownfield/change-request", json={
        "repo_url": "https://github.com/octocat/Hello-World",
        "description": "Add a note to the README.",
    })
    assert r.status_code == 201
    token = r.json()["token"]
    assert token and token in gs._pending_change_requests


def test_the_queued_entry_carries_the_defaults(client):
    r = client.post("/api/brownfield/change-request", json={
        "repo_url": "https://github.com/octocat/Hello-World",
        "description": "Add a note to the README.",
    })
    token = r.json()["token"]
    pending = gs._pending_change_requests[token]
    assert pending["repo_url"] == "https://github.com/octocat/Hello-World"
    assert pending["change_request"]["kind"] == "modification"
    assert pending["change_request"]["description"] == "Add a note to the README."
    assert pending["change_request"]["title"]          # falls back to the description
    assert pending["base_ref"] == ""


@pytest.mark.parametrize("repo_url", [
    "not-a-url",
    "https://gitlab.com/owner/repo",
    "git@github.com:owner/repo.git",
    "https://github.com/owner/repo/../../etc",
])
def test_a_disallowed_repo_url_is_rejected_before_a_token_is_issued(client, repo_url):
    r = client.post("/api/brownfield/change-request", json={
        "repo_url": repo_url, "description": "do something",
    })
    assert r.status_code == 400
    assert gs._pending_change_requests == {}


def test_an_empty_description_is_rejected(client):
    r = client.post("/api/brownfield/change-request", json={
        "repo_url": "https://github.com/octocat/Hello-World", "description": "   ",
    })
    assert r.status_code == 400
    assert gs._pending_change_requests == {}


def test_an_invalid_kind_is_rejected(client):
    r = client.post("/api/brownfield/change-request", json={
        "repo_url": "https://github.com/octocat/Hello-World",
        "description": "x", "kind": "refactor",
    })
    assert r.status_code == 400


def test_acceptance_criteria_and_constraints_are_trimmed_and_blanks_dropped(client):
    r = client.post("/api/brownfield/change-request", json={
        "repo_url": "https://github.com/octocat/Hello-World",
        "description": "x",
        "acceptance_criteria": ["  a real one  ", "", "   "],
        "constraints": ["also real", ""],
    })
    token = r.json()["token"]
    cr = gs._pending_change_requests[token]["change_request"]
    assert cr["acceptance_criteria"] == ["a real one"]
    assert cr["constraints"] == ["also real"]


# --------------------------------------------------------------- the stream's own gate

def test_an_unknown_token_yields_a_clean_error_event(client):
    with client.stream("GET", "/api/brownfield/stream",
                       params={"token": "nope", "project": "p"}) as r:
        body = "".join(r.iter_text())
    assert "Unknown or already-used token" in body


def test_a_token_is_consumed_by_the_stream_that_claims_it(client, monkeypatch):
    # No network: fail the clone step itself so the run finishes (with a clean, reported error)
    # without reaching github.com. The token-pop this test cares about happens synchronously in
    # _run_events_brownfield, before the clone is ever attempted.
    def fake_run_command(self, cmd, **kw):
        if cmd[:2] == ["git", "clone"]:
            return RunResult(stdout="", stderr="fake: no network in this test", exit_code=1)
        return RunResult(stdout="", stderr="", exit_code=0)

    monkeypatch.setattr(LocalDiskExecutor, "run_command", fake_run_command)

    token = client.post("/api/brownfield/change-request", json={
        "repo_url": "https://github.com/octocat/Hello-World", "description": "x",
    }).json()["token"]
    assert token in gs._pending_change_requests

    with client.stream("GET", "/api/brownfield/stream", params={"token": token, "project": "p"}) as r:
        body = "".join(r.iter_text())

    assert token not in gs._pending_change_requests
    assert '"type": "brownfield_step"' in body
    assert '"node": "acquire"' in body and '"status": "failed"' in body


def test_an_empty_project_is_rejected(client):
    token = client.post("/api/brownfield/change-request", json={
        "repo_url": "https://github.com/octocat/Hello-World", "description": "x",
    }).json()["token"]
    r = client.get("/api/brownfield/stream", params={"token": token, "project": ""})
    assert r.status_code == 400
