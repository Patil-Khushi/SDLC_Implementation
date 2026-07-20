"""429 retry-with-backoff, shared by LLMGateway.complete (streaming) and complete_with_tools
(the repair path's tool-use loop) via ``_with_rate_limit_retry``.

Regression coverage for a PR #2 review finding: the original resilience work only wrapped
``complete()``'s streaming call, leaving ``complete_with_tools()`` to call the blocking
``client.messages.create()`` directly with no retry — a rate limit hit during repair failed
immediately. Both paths now share one retry helper.
"""

from __future__ import annotations

import httpx
import pytest

from app.services import llm_gateway as gw_module
from app.services.llm_gateway import _retry_after_seconds, _with_rate_limit_retry


def _rate_limit_error(retry_after: str | None = None) -> Exception:
    import anthropic

    request = httpx.Request("POST", "https://example.com")
    headers = {"retry-after": retry_after} if retry_after else {}
    response = httpx.Response(429, headers=headers, request=request)
    return anthropic.RateLimitError("rate limited", response=response, body=None)


def test_retry_after_seconds_reads_the_header() -> None:
    assert _retry_after_seconds(_rate_limit_error("2"), default=15.0) == 3.0  # +1.0 margin


def test_retry_after_seconds_falls_back_to_default_with_no_signal() -> None:
    assert _retry_after_seconds(_rate_limit_error(), default=15.0) == 15.0


def test_with_rate_limit_retry_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(gw_module.time, "sleep", sleeps.append)

    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _rate_limit_error("0")
        return "ok"

    assert _with_rate_limit_retry(flaky, max_attempts=5) == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2  # one sleep per failed attempt before the third (successful) call


def test_with_rate_limit_retry_raises_after_exhausting_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw_module.time, "sleep", lambda _s: None)

    def always_limited() -> str:
        raise _rate_limit_error("0")

    with pytest.raises(gw_module.anthropic.RateLimitError):
        _with_rate_limit_retry(always_limited, max_attempts=3)


def test_with_rate_limit_retry_does_not_swallow_other_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw_module.time, "sleep", lambda _s: None)

    def boom() -> str:
        raise ValueError("not a rate limit")

    with pytest.raises(ValueError):
        _with_rate_limit_retry(boom, max_attempts=5)
