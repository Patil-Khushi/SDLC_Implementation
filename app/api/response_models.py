"""Response models for the implementation API."""

from pydantic import BaseModel, Field


class StartResponse(BaseModel):
    project_id: str
    workflow_status: str
    run_id: str = ""
    # Workspace-relative paths of the files produced this run.
    generated_code: list[str] = Field(default_factory=list)
    # Workspace-relative paths of unit test files written this run.
    unit_tests: list[str] = Field(default_factory=list)
    # The Code Review agent's Markdown report, and where it was saved (reports/<project>-<run>.md).
    # Empty when the run escalated to human review before the final review stage ran.
    review_report: str = ""
    review_report_path: str = ""
