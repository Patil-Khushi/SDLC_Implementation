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
