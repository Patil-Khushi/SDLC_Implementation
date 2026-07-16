"""Smoke test of the /implementation/start route + compiled graph.

Every run scaffolds the repo-root boilerplate first (no LLM), then — even with zero work
items — proceeds straight to the batch_review interrupt rather than "completed", since a commit
now always waits for a human decision. Real code generation is covered by test_code_generator.py;
the repair/rework/commit loop is covered by test_graph.py. This test just proves the route +
graph wire up cleanly, with a FakeExecutor standing in for the sandbox (no LLM or real sandbox
needed).
"""

from fastapi.testclient import TestClient

from app.integrations.executor import FakeExecutor, set_executor
from app.main import app

client = TestClient(app)


def test_start_route_runs_cleanly() -> None:
    set_executor(FakeExecutor())
    try:
        response = client.post(
            "/implementation/start",
            json={"project_id": "p1", "design_package": {"SKILL.md": "conventions"}},
        )
    finally:
        set_executor(None)

    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == "p1"
    assert body["run_id"]                              # a run id was assigned
    assert len(body["generated_code"]) == 7             # scaffold's boilerplate files, no work items
    assert body["workflow_status"] == "pending_review"  # scaffold done -> straight to batch_review
