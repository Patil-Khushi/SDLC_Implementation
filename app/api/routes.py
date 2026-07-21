"""REST API routes. FastAPI validates the request and calls the LangGraph
workflow; it contains no agent logic itself.
"""

import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from app.api.request_models import StartRequest
from app.api.response_models import StartResponse
from app.graph.graph import workflow
from app.graph.state import WorkflowState, new_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/implementation", tags=["implementation"])


@router.post("/start", response_model=StartResponse)
def start(request: StartRequest) -> StartResponse:
    """Run the implementation workflow for a design package."""
    run_id = uuid4().hex
    initial: WorkflowState = new_state(
        run_id=run_id,
        attempt=request.attempt,
        project_id=request.project_id,
        design_package=request.design_package,
    )
    initial["workflow_status"] = "started"

    # The graph runs to completion with no human-in-the-loop: a completed plan auto-commits, and
    # a repair-cap failure ends the run flagged ``needs_human_review``. The checkpointer is kept
    # only so ``get_state`` can read the finished run; the workflow makes synchronous LLM calls,
    # so surface failures as a clean 502.
    # TODO: move to background execution + GET /status/{project_id} once the pipeline grows.
    # ~3 supersteps per work item, so 100 capped a run at ~30 items (a large module-per-item plan
    # would raise GraphRecursionError mid-run). 1000 matches scripts/run_fixture.py.
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 1000}
    try:
        workflow.invoke(initial, config)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Workflow failed for project %s", request.project_id)
        raise HTTPException(status_code=502, detail="Implementation workflow failed") from exc

    final = workflow.get_state(config).values
    return StartResponse(
        project_id=final.get("project_id", request.project_id),
        workflow_status=final.get("workflow_status", "completed"),
        run_id=final.get("run_id", run_id),
        generated_code=final.get("generated_code") or [],
        unit_tests=final.get("unit_tests") or [],
        review_report=final.get("review_report") or "",
        review_report_path=final.get("review_report_path") or "",
    )
