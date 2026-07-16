"""Contract models round-trip, reject junk, and stay in sync with the published schemas."""

import json

import pytest
from pydantic import BaseModel, ValidationError

from app.models import (
    CONTRACT_MODELS,
    GenerationMetrics,
    GenerationSummary,
    WorkItem,
    contracts_dir,
    schema_text,
)

INSTANCES: list[BaseModel] = [
    WorkItem(
        id="WI-001",
        requirement_ids=["REQ-1"],
        endpoints=["POST /login"],
        tables=["users"],
        screens=["Login"],
        target_files=["app/api/login.py"],
    ),
    GenerationSummary(work_item_id="WI-001", files_produced=["app/api/login.py"], compile_passed=True),
    GenerationMetrics(files_produced=2, compile_passes=1, compile_failures=1, repairs_used=3),
]
_ids = [type(o).__name__ for o in INSTANCES]


@pytest.mark.parametrize("obj", INSTANCES, ids=_ids)
def test_python_round_trip(obj: BaseModel) -> None:
    assert type(obj).model_validate(obj.model_dump()) == obj


@pytest.mark.parametrize("obj", INSTANCES, ids=_ids)
def test_json_round_trip(obj: BaseModel) -> None:
    assert type(obj).model_validate_json(obj.model_dump_json()) == obj


@pytest.mark.parametrize("obj", INSTANCES, ids=_ids)
def test_rejects_unknown_fields(obj: BaseModel) -> None:
    payload = obj.model_dump()
    payload["totally_unexpected"] = 1
    with pytest.raises(ValidationError):
        type(obj).model_validate(payload)


def test_metrics_from_summaries_is_file_consistent() -> None:
    summaries = [
        GenerationSummary(work_item_id="A", files_produced=["a.py", "b.py"], compile_passed=True, repairs_used=1),
        GenerationSummary(work_item_id="B", files_produced=["c.py"], compile_passed=False, repairs_used=3),
    ]
    m = GenerationMetrics.from_summaries(summaries, {"A": 2.0, "B": 5.0})
    assert m.files_produced == 3
    assert m.compile_passes == 2
    assert m.compile_failures == 1
    assert m.compile_passes + m.compile_failures == m.files_produced   # denominator invariant
    assert m.repairs_used == 4
    assert m.seconds_per_item == {"A": 2.0, "B": 5.0}


@pytest.mark.parametrize("stem", list(CONTRACT_MODELS), ids=list(CONTRACT_MODELS))
def test_published_schema_matches_model(stem: str) -> None:
    """Committed JSON Schemas must not drift from the models. Regenerate: `python -m app.models`."""
    path = contracts_dir() / f"{stem}.schema.json"
    assert path.exists(), f"missing {path}; run `python -m app.models`"
    on_disk = path.read_text(encoding="utf-8")
    assert json.loads(on_disk)  # valid JSON
    assert on_disk == schema_text(CONTRACT_MODELS[stem]), f"{stem}.schema.json is stale; run `python -m app.models`"
