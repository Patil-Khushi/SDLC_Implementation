"""GenerationSummary contract model.

One ``GenerationSummary`` is emitted per :class:`~app.models.work_item.WorkItem`: the per-item
record of what the Code Generation agent did — files written, whether the item cleared the
compile gate, and how many local repair attempts it consumed. These roll up into
:class:`~app.models.generation_metrics.GenerationMetrics`. Acceptance ("% accepted without
rewrite") is NOT decided here — the review step joins that later.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GenerationSummary(BaseModel):
    """Per-work-item outcome of code generation."""

    model_config = ConfigDict(extra="forbid")

    work_item_id: str = Field(min_length=1, description="Id of the WorkItem this summarizes.")
    files_produced: list[str] = Field(
        default_factory=list, description="Workspace-relative paths written for this item."
    )
    compile_passed: bool | None = Field(
        default=None,
        description="Gate verdict: True=all files compiled, False=escalated, None=not evaluated "
        "yet. Code generation leaves this None; the gate/repair nodes fill it in.",
    )
    repairs_used: int = Field(
        default=0, ge=0, description="Local repair attempts consumed (0 == compiled without rewrite)."
    )
