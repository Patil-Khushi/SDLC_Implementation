"""Request models for the implementation API."""

from typing import Any

from pydantic import BaseModel, Field


class StartRequest(BaseModel):
    project_id: str
    # The design pack: named artifacts (openapi.yaml, schema.sql, validation-rules.json, ...)
    # keyed by name. See app/graph/state.py :: WorkflowState.design_package.
    design_package: dict[str, Any] = Field(default_factory=dict)
    # The orchestrator's attempt number; echoed unchanged through the run (default 0 for
    # direct/manual calls). This service never increments it.
    attempt: int = 0


class ReviewRequest(BaseModel):
    """The human decision that resumes a run paused at ``batch_review`` (app/graph/nodes.py).

    ``approved=True`` commits the whole run. ``approved=False`` sends the named work items back
    through the repair path with the given feedback (``rejections``); items not named here are
    left untouched — the run pauses again at batch_review once the rework is re-gated.
    """

    approved: bool
    rejections: dict[str, str] = Field(default_factory=dict)
