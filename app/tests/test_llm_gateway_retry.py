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
    assert sleeps == [2.0]  # exponential backoff (2, 4, 8, … capped 30), not the 429 retry-after path


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


# --------------------------------------------------------------------------- server-side errors
#
# The live abort this widening fixes: a 529 ``overloaded_error`` mid-run killed a code-generation
# graph after many files had already been generated. It arrived as a plain ``APIStatusError``, which
# matched NEITHER of the old except clauses (RateLimitError is a SUBCLASS of APIStatusError, so
# catching the child never catches the parent; APIConnectionError is a SIBLING, not a parent), so it
# propagated straight out of the retry loop. Retryability is now decided by class + HTTP status, so
# every transient flavor is covered without enumerating them one patch at a time.


def _status_error(
    code: int, error_type: str = "", cls: type[Exception] | None = None
) -> Exception:
    import anthropic

    request = httpx.Request("POST", "https://example.com")
    body = {"type": "error", "error": {"type": error_type, "message": "boom"}}
    return (cls or anthropic.APIStatusError)(
        "boom", response=httpx.Response(code, request=request), body=body
    )


@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        (529, "overloaded_error"),   # the exact live failure
        (500, "api_error"),
        (502, ""),
        (503, "overloaded_error"),
        (504, ""),
        (408, ""),                   # request timeout
        (409, ""),                   # conflict
        (425, ""),                   # too early
    ],
)
def test_transient_status_errors_are_retried(
    monkeypatch: pytest.MonkeyPatch, code: int, error_type: str
) -> None:
    monkeypatch.setattr(gw_module.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def overloaded_once() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(code, error_type)
        return "ok"

    assert _with_rate_limit_retry(overloaded_once, max_attempts=3) == "ok"
    assert calls["n"] == 2


@pytest.mark.parametrize(
    ("code", "cls_name"),
    [
        (400, "BadRequestError"),
        (401, "AuthenticationError"),
        (403, "PermissionDeniedError"),
        (404, "NotFoundError"),
        (422, "UnprocessableEntityError"),
    ],
)
def test_permanent_client_errors_are_not_retried(
    monkeypatch: pytest.MonkeyPatch, code: int, cls_name: str
) -> None:
    """A 4xx won't succeed on retry — looping only burns time/tokens and hides a real bug, so it
    must surface on the FIRST attempt with its original type."""
    import anthropic

    monkeypatch.setattr(gw_module.time, "sleep", lambda _s: None)
    cls = getattr(anthropic, cls_name)
    calls = {"n": 0}

    def bad_request() -> str:
        calls["n"] += 1
        raise _status_error(code, "invalid_request_error", cls=cls)

    with pytest.raises(cls):
        _with_rate_limit_retry(bad_request, max_attempts=5)
    assert calls["n"] == 1  # failed fast — no retry budget spent


def test_overloaded_raises_after_exhausting_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic

    monkeypatch.setattr(gw_module.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def always_overloaded() -> str:
        calls["n"] += 1
        raise _status_error(529, "overloaded_error")

    with pytest.raises(anthropic.APIStatusError):
        _with_rate_limit_retry(always_overloaded, max_attempts=3)
    assert calls["n"] == 3  # used the full budget before giving up


def test_backoff_is_exponential_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Retry-After header (529s don't send one) -> exponential 2,4,8,16 capped at 30s."""
    sleeps: list[float] = []
    monkeypatch.setattr(gw_module.time, "sleep", sleeps.append)

    def always_overloaded() -> str:
        raise _status_error(529, "overloaded_error")

    with pytest.raises(Exception):
        _with_rate_limit_retry(always_overloaded, max_attempts=7)
    assert sleeps == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]  # doubling, then capped


def test_retry_after_header_still_wins_over_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the server tells us how long to wait (429s do), honor it instead of the exponential."""
    sleeps: list[float] = []
    monkeypatch.setattr(gw_module.time, "sleep", sleeps.append)
    calls = {"n": 0}

    def limited_once() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _rate_limit_error("5")
        return "ok"

    assert _with_rate_limit_retry(limited_once, max_attempts=3) == "ok"
    assert sleeps == [6.0]  # header 5s + 1s margin, not the exponential 2s


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: httpx.ConnectTimeout("connect timed out"),
        lambda: httpx.ReadTimeout("read timed out"),
        lambda: httpx.PoolTimeout("pool timed out"),
        lambda: httpx.ConnectError("connection refused"),
        lambda: httpx.ProxyError("proxy blew up"),
    ],
)
def test_every_httpx_transport_error_is_retried(
    monkeypatch: pytest.MonkeyPatch, exc_factory
) -> None:
    """Previously only ReadError/WriteError/RemoteProtocolError were listed; keying on the
    httpx.TransportError BASE covers every connect/read/pool/proxy/timeout variant."""
    monkeypatch.setattr(gw_module.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc_factory()
        return "ok"

    assert _with_rate_limit_retry(flaky, max_attempts=3) == "ok"
    assert calls["n"] == 2
