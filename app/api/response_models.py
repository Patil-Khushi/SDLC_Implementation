"""Response models for the implementation API."""

from pydantic import BaseModel, Field


class StartResponse(BaseModel):
    project_id: str
    workflow_status: str
    run_id: str = ""
    # Workspace-relative paths of the files produced this run.
    generated_code: list[str] = Field(default_factory=list)
