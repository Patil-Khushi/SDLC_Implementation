"""REST API routes. FastAPI validates the request and calls the LangGraph
workflow; it contains no agent logic itself.
"""

import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from langgraph.types import Command

from app.api.request_models import ReviewRequest, StartRequest
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

    # The graph is compiled with a checkpointer (for the HITL interrupt), so invoke needs a
    # thread id. The workflow makes synchronous LLM calls; surface failures as a clean 502.
    # TODO: move to background execution + GET /status/{project_id} once the pipeline grows.
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 100}
    try:
        workflow.invoke(initial, config)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Workflow failed for project %s", request.project_id)
        raise HTTPException(status_code=502, detail="Implementation workflow failed") from exc

    # Read the current state (also correct when the run paused at a batch_review/human_review
    # interrupt).
    final = workflow.get_state(config).values
    return StartResponse(
        project_id=final.get("project_id", request.project_id),
        workflow_status=final.get("workflow_status", "completed"),
        run_id=final.get("run_id", run_id),
        generated_code=final.get("generated_code") or [],
    )


@router.post("/{run_id}/review", response_model=StartResponse)
def review(run_id: str, request: ReviewRequest) -> StartResponse:
    """Resume a run paused at the batch_review interrupt with a human decision.

    ``approved=True`` resumes straight through to the single run-level commit.
    ``approved=False`` resumes into the rework loop for the named ``rejections`` and the run
    pauses again at batch_review once they're re-generated and re-gated.
    """
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 100}
    if not workflow.get_state(config).values:
        raise HTTPException(status_code=404, detail=f"no run found for run_id {run_id!r}")

    decision = {"approved": request.approved, "rejections": request.rejections}
    try:
        workflow.invoke(Command(resume=decision), config)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Review resume failed for run %s", run_id)
        raise HTTPException(status_code=502, detail="Implementation workflow failed") from exc

    final = workflow.get_state(config).values
    return StartResponse(
        project_id=final.get("project_id", ""),
        workflow_status=final.get("workflow_status", "completed"),
        run_id=final.get("run_id", run_id),
        generated_code=final.get("generated_code") or [],
    )
