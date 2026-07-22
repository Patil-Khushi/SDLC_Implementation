"""429 retry-with-backoff, shared by LLMGateway.complete (streaming) and complete_with_tools
(the repair path's tool-use loop) via ``_with_rate_limit_retry``.

Regression coverage for a PR #2 review finding: the original resilience work only wrapped
``complete()``'s streaming call, leaving ``complete_with_tools()`` to call the blocking
``client.messages.create()`` directly with no retry — a rate limit hit during repair failed
immediately. Both paths now share one retry helper.

Also covers transient network drops: a mid-stream connection reset surfaces as a RAW
``httpx.ReadError`` (e.g. WinError 10054) — NOT ``anthropic.APIConnectionError``, which the
SDK only raises for failures while ESTABLISHING the request. The original retry caught only
``RateLimitError``, so one dropped socket 100+ calls into a code-generation run crashed the
whole graph (seen live: ``httpx.ReadError`` during the resources-app backend-config item).
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


@pytest.mark.parametrize("exc_type", [httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError])
def test_transient_network_error_is_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, exc_type: type[Exception]
) -> None:
    # The live failure mode: a mid-stream connection reset (WinError 10054) raises a raw httpx
    # error. One drop must not kill a multi-call run — the retry re-opens the stream from scratch.
    sleeps: list[float] = []
    monkeypatch.setattr(gw_module.time, "sleep", sleeps.append)

    calls = {"n": 0}

    def drops_once() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc_type("[WinError 10054] An existing connection was forcibly closed")
        return "ok"

    assert _with_rate_limit_retry(drops_once, max_attempts=3) == "ok"
    assert calls["n"] == 2
    assert sleeps == [10.0]  # fixed transient backoff, not the 429 retry-after path


def test_transient_network_error_raises_after_exhausting_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gw_module.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def always_drops() -> str:
        calls["n"] += 1
        raise httpx.ReadError("connection reset")

    with pytest.raises(httpx.ReadError):
        _with_rate_limit_retry(always_drops, max_attempts=3)
    assert calls["n"] == 3  # tried the full budget before giving up


def test_api_connection_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    # Failures while ESTABLISHING the request are wrapped by the SDK — also transient.
    monkeypatch.setattr(gw_module.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def refused_once() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise gw_module.anthropic.APIConnectionError(
                request=httpx.Request("POST", "https://example.com")
            )
        return "ok"

    assert _with_rate_limit_retry(refused_once, max_attempts=3) == "ok"
