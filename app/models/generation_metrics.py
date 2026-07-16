"""GenerationMetrics contract model — the ``generation-metrics.json`` shape.

Run-level rollup of the per-item :class:`~app.models.generation_summary.GenerationSummary`
records. ``files_produced`` is the DENOMINATOR for the "% accepted without rewrite" metric;
this agent deliberately does NOT compute that percentage — the review step joins acceptance
later. Counts are file-level and internally consistent:
``compile_passes + compile_failures == files_produced``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.models.generation_summary import GenerationSummary


class GenerationMetrics(BaseModel):
    """Aggregate code-generation metrics for one run."""

    model_config = ConfigDict(extra="forbid")

    files_produced: int = Field(
        default=0, ge=0, description="Total files written. DENOMINATOR for '% accepted without rewrite'."
    )
    compile_passes: int = Field(
        default=0, ge=0, description="Files belonging to work items that cleared the gate."
    )
    compile_failures: int = Field(
        default=0, ge=0, description="Files belonging to escalated work items."
    )
    repairs_used: int = Field(
        default=0, ge=0, description="Total local repair attempts across every work item."
    )
    seconds_per_item: dict[str, float] = Field(
        default_factory=dict, description="Wall-clock seconds per work item, keyed by WorkItem.id."
    )

    @classmethod
    def from_summaries(
        cls,
        summaries: list[GenerationSummary],
        seconds_per_item: dict[str, float] | None = None,
    ) -> "GenerationMetrics":
        """Roll per-item summaries up into run-level metrics. Deterministic; no acceptance calc."""
        files_produced = sum(len(s.files_produced) for s in summaries)
        compile_passes = sum(len(s.files_produced) for s in summaries if s.compile_passed)
        return cls(
            files_produced=files_produced,
            compile_passes=compile_passes,
            compile_failures=files_produced - compile_passes,
            repairs_used=sum(s.repairs_used for s in summaries),
            seconds_per_item=dict(seconds_per_item or {}),
        )
