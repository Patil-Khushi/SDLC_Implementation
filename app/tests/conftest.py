"""Shared pytest fixtures wired to the REAL design-pack fixtures.

No stub packs are created here — the packs live at the repo's ``fixtures/`` directory (override
with the ``FIXTURES_DIR`` env var). LLM responses are recorded once (``RECORD=1``) then replayed,
so runs are deterministic and cost no tokens.
"""

from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from app.models import WorkItem
from app.services.llm_gateway import FakeLLMGateway, LLMGateway

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parents[3]  # tests -> app -> implementation -> services -> repo
_RESPONSES_DIR = _TESTS_DIR / "fixtures" / "llm-responses"
_PLAN_PATH = _TESTS_DIR / "fixtures" / "implementation-plan.ecommerce.json"


def _fixtures_root() -> Path:
    return Path(os.environ.get("FIXTURES_DIR", str(_REPO_ROOT / "fixtures")))


def _load_pack(pack_dir: Path) -> dict[str, Any]:
    """Load a pack's top-level artifacts into a name -> content dict (.json parsed, else text)."""
    package: dict[str, Any] = {}
    for path in sorted(pack_dir.iterdir()):
        if not path.is_file():
            continue  # skip assets/ etc.
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            try:
                package[path.name] = json.loads(text)
                continue
            except json.JSONDecodeError:
                pass
        package[path.name] = text
    return package


class ReplayGateway(FakeLLMGateway):
    """Records-once / replays LLM responses keyed by work-item id (inferred from the prompt).

    RECORD=1: call the real gateway once per key and save under ``llm-responses/<key>.txt``.
    Otherwise: replay the saved response; a missing key fails loudly ("run RECORD=1 first").
    ``use(key)`` pins the key explicitly (e.g. for variant payloads like ``login-backend.broken1``).
    """

    def __init__(self, store_dir: Path, *, record: bool = False) -> None:
        super().__init__()
        self._store = Path(store_dir)
        self._record = record
        self._current_key: str | None = None
        self._key_queue: deque[str] = deque()
        self._real: LLMGateway | None = None

    def use(self, key: str) -> "ReplayGateway":
        """Pin a single key served for every call (e.g. always the '.broken1' variant)."""
        self._current_key = key
        return self

    def use_sequence(self, keys: list[str]) -> "ReplayGateway":
        """Serve these keys in order across successive calls (e.g. ['...broken1', '...fixed'])."""
        self._key_queue = deque(keys)
        return self

    @staticmethod
    def _key_from_prompt(prompt: str) -> str:
        import re

        match = re.search(r"Work item:\s*(\S+)", prompt)
        return match.group(1) if match else "default"

    def _next_key(self, prompt: str) -> str:
        if self._key_queue:
            return self._key_queue.popleft()
        return self._current_key or self._key_from_prompt(prompt)

    def complete(self, prompt: str, *, system: str | None = None, **kwargs: Any) -> str:
        self.calls.append({"prompt": prompt, "system": system, "kwargs": kwargs})
        key = self._next_key(prompt)
        path = self._store / f"{key}.txt"
        if self._record:
            if self._real is None:
                self._real = LLMGateway()
            response = self._real.complete(prompt, system=system)
            self._store.mkdir(parents=True, exist_ok=True)
            path.write_text(response, encoding="utf-8")
            return response
        if path.exists():
            return path.read_text(encoding="utf-8")
        raise AssertionError(f"no recorded LLM response for key {key!r} at {path} — run RECORD=1 first")

    def complete_with_tools(
        self, prompt: str, *, system: str | None = None, tools: list | None = None, max_iters: int = 4
    ) -> str:
        return self.complete(prompt, system=system)


@pytest.fixture
def dummy_pack_complete() -> Path:
    return _fixtures_root() / "ecommerce_complete"


@pytest.fixture
def dummy_pack_missing() -> Path:
    return _fixtures_root() / "ecommerce_missing_mandatory"


@pytest.fixture
def design_package(dummy_pack_complete: Path) -> dict[str, Any]:
    return _load_pack(dummy_pack_complete)


@pytest.fixture
def dummy_plan() -> list[WorkItem]:
    data = json.loads(_PLAN_PATH.read_text(encoding="utf-8"))
    return [WorkItem(**item) for item in data["work_items"]]


@pytest.fixture
def fake_gateway() -> ReplayGateway:
    return ReplayGateway(_RESPONSES_DIR, record=bool(os.environ.get("RECORD")))


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture
def mcp_executor():  # real executor — @integration only
    import asyncio

    from app.config.settings import get_settings
    from app.integrations.executor import MCPExecutor

    settings = get_settings()
    try:
        return asyncio.run(MCPExecutor.connect(settings.sandbox_mcp_url, settings.sandbox_mcp_transport))
    except Exception as exc:  # noqa: BLE001 - sandbox not up
        pytest.skip(f"exec-sandbox not reachable: {exc}")
