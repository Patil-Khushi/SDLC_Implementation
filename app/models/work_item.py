"""WorkItem contract model.

A ``WorkItem`` is one unit of code-generation work. The Design Package is decomposed into a
list of work items; the Code Generation agent processes them one at a time. Each item records
*what it covers* (traceability) and *what it must produce* (target files).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class WorkItem(BaseModel):
    """A single, independently generatable unit of work."""

    # Published contract → reject unknown keys so typos/drift fail loudly.
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="Stable id, e.g. 'WI-001'. Join key for summaries/metrics.")
    feature_id: str = Field(
        default="", description="User-feature this item belongs to, e.g. '4.1' (from user_features.json) "
        "or 'F-02' (from user-features.md). Items sharing a feature_id are committed together as ONE "
        "feat(...) commit. Empty = ungrouped (committed on its own, keyed by id)."
    )
    feature_title: str = Field(
        default="", description="Human-readable feature name for the commit subject, e.g. "
        "'User Registration and Authentication'."
    )
    requirement_ids: list[str] = Field(
        default_factory=list, description="REQ IDs this work item implements (traceability)."
    )
    endpoints: list[str] = Field(
        default_factory=list, description="API endpoints covered, e.g. 'POST /login' (FastAPI)."
    )
    tables: list[str] = Field(
        default_factory=list, description="Database tables/entities this work item touches."
    )
    screens: list[str] = Field(
        default_factory=list, description="UI screens covered (React/TS)."
    )
    target_files: list[str] = Field(
        default_factory=list, description="Workspace-relative file paths this item should produce."
    )
    file_specs: dict[str, str] = Field(
        default_factory=dict,
        description="Optional per-target-file spec (path -> what the file must contain), taken "
        "verbatim from the design package's structure tree. Grounds generation of files that "
        "aren't tied to a single endpoint/screen (app entrypoints, config, middleware, stores).",
    )
