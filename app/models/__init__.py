"""Contract models for IMP-001 (implementation → testing).

These pydantic models are the source of truth. The JSON Schemas published under
``contracts/implementation-to-testing/*.schema.json`` are GENERATED from them via
:func:`export_json_schemas` (run ``python -m app.models`` to regenerate). ``test_contracts.py``
fails if the two drift.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from app.models.generation_metrics import GenerationMetrics
from app.models.generation_summary import GenerationSummary
from app.models.work_item import WorkItem

__all__ = [
    "WorkItem",
    "GenerationSummary",
    "GenerationMetrics",
    "CONTRACT_MODELS",
    "contracts_dir",
    "schema_text",
    "export_json_schemas",
]

# schema filename stem -> model. Published as contracts/implementation-to-testing/<stem>.schema.json
CONTRACT_MODELS: dict[str, type[BaseModel]] = {
    "work-item": WorkItem,
    "generation-summary": GenerationSummary,
    "generation-metrics": GenerationMetrics,
}


def contracts_dir() -> Path:
    """Absolute path to contracts/implementation-to-testing/ (models→app→implementation→services→repo)."""
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / "contracts" / "implementation-to-testing"


def schema_text(model: type[BaseModel]) -> str:
    """Canonical JSON-Schema text for a model (stable formatting for drift comparison)."""
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"


def export_json_schemas(dest: Path | None = None) -> dict[str, Path]:
    """Write each contract model's JSON Schema to ``dest`` (default: the contracts dir)."""
    target = dest or contracts_dir()
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for stem, model in CONTRACT_MODELS.items():
        path = target / f"{stem}.schema.json"
        path.write_text(schema_text(model), encoding="utf-8")
        written[stem] = path
    return written
