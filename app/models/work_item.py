"""WorkItem contract model.

A ``WorkItem`` is one unit of code-generation work. The Design Package is decomposed into a
list of work items; the Code Generation agent processes them one at a time. Each item records
*what it covers* (traceability) and *what it must produce* (target files).
"""

from __future__ import annotations

from typing import Literal

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

    # --- brownfield: changing an EXISTING codebase rather than producing a new one -------------
    # All three default to today's behaviour, so a design-pack plan that sets none of them means
    # exactly what it always did (action="create"). Additive-with-defaults is what keeps this
    # backward-compatible under extra="forbid": old payloads still validate, and a consumer that
    # ignores the new keys is unaffected.
    action: Literal["create", "modify", "delete"] = Field(
        default="create",
        description="What this item does to its target_files. 'create' (the default, and what a "
        "design-pack plan always means) writes files that do not exist yet. 'modify' edits files "
        "that DO exist — the distinction the completeness gate cannot make on its own, since an "
        "already-present file passes files_complete without being touched. 'delete' removes them.",
    )
    change_intent: str = Field(
        default="",
        description="For a brownfield item: what must change in these files and why, in prose. "
        "The per-item slice of the change request, and the editing agent's actual instruction — "
        "file_specs describes what a file must CONTAIN, this describes what must CHANGE about it.",
    )
    acceptance_criteria: list[str] = Field(
        default_factory=list,
        description="Observable conditions that make this item done, carried from the change "
        "request so generated tests can target the requested behaviour rather than just the "
        "module's existing surface.",
    )
