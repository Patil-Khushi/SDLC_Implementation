"""A dry-run must not poison the rest of the process.

``llm_gateway.llm_gateway`` is a singleton, and every agent binds its methods at construction
(``app/agents/base.py``). The dry-run path replaces those methods with canned-reply stubs. Before
this was fixed, nothing restored them: one dry-run through ``/api/run/stream`` left every later
*real* run in the same server process silently writing canned placeholder files, and
``/api/plan/revise`` returning canned JSON that parsed as "the planner did not return a valid plan".
The only reset was restarting the server, and nothing in the UI hinted at it.

What these assert is that the gateway is left EXACTLY as it was found — compared as a snapshot of
the instance ``__dict__``, not by bound-method identity (reading ``instance.method`` builds a fresh
bound object every time, so identity there is meaningless).

Deliberately not "pristine": ``monkeypatch.setattr`` restores by re-``setattr``, so any earlier
test in the suite that patched the gateway leaves a permanent instance attribute behind. Restoring
to the found state is the real contract, and it is the one that holds whether this file runs alone
or after the rest of the suite.
"""

from __future__ import annotations

import pytest

from app.services import llm_gateway

gs = pytest.importorskip("scripts.graph_server")


_PATCHED = ("complete", "complete_with_tools")


def _snapshot() -> dict:
    """Exactly what the gateway instance overrides right now, if anything."""
    d = llm_gateway.llm_gateway.__dict__
    return {name: d[name] for name in _PATCHED if name in d}


def test_the_patch_is_reverted_on_a_clean_exit():
    before = _snapshot()

    with gs.dry_run_gateway():
        assert llm_gateway.llm_gateway.complete is gs._canned_llm_reply, "the patch must apply"

    assert _snapshot() == before


def test_the_patch_is_reverted_when_the_run_raises():
    # The case that actually bit: a dry-run that failed left the process poisoned.
    before = _snapshot()

    with pytest.raises(RuntimeError):
        with gs.dry_run_gateway():
            raise RuntimeError("the run exploded")

    assert _snapshot() == before


def test_nesting_restores_all_the_way_out():
    before = _snapshot()
    with gs.dry_run_gateway():
        with gs.dry_run_gateway():
            pass
        assert llm_gateway.llm_gateway.complete is gs._canned_llm_reply, "outer scope still patched"
    assert _snapshot() == before


def test_an_existing_instance_override_is_restored_not_deleted():
    # A caller may legitimately have injected its own stub (conftest and several suites do exactly
    # this via monkeypatch); restoring must put THAT back, not drop it along with the dry-run patch.
    before = _snapshot()
    sentinel = lambda *a, **k: "injected"  # noqa: E731
    llm_gateway.llm_gateway.complete = sentinel  # type: ignore[method-assign]
    try:
        with gs.dry_run_gateway():
            assert llm_gateway.llm_gateway.complete is gs._canned_llm_reply
        assert llm_gateway.llm_gateway.complete is sentinel, "the injected stub survives"
    finally:
        if "complete" in before:
            llm_gateway.llm_gateway.__dict__["complete"] = before["complete"]
        else:
            llm_gateway.llm_gateway.__dict__.pop("complete", None)
    assert _snapshot() == before


def test_no_override_is_invented_when_there_was_none():
    # Restoring by re-assigning a bound method read from the class would leave a permanent,
    # redundant instance attribute — pristine-looking but not pristine. Deleting is correct.
    llm_gateway.llm_gateway.__dict__.pop("complete", None)
    llm_gateway.llm_gateway.__dict__.pop("complete_with_tools", None)
    with gs.dry_run_gateway():
        pass
    assert _snapshot() == {}


def test_inside_the_patch_the_gateway_serves_canned_replies():
    with gs.dry_run_gateway():
        reply = llm_gateway.llm_gateway.complete("generate the login controller")
        assert isinstance(reply, str) and reply.strip(), "dry-run must return something parseable"
        # the tool-calling surface is patched too, or a tool-using agent would hit the real API
        tool_reply = llm_gateway.llm_gateway.complete_with_tools("generate the login controller")
        assert isinstance(tool_reply, str) and tool_reply.strip()


def test_drive_holds_the_patch_object_so_the_finally_can_release_it():
    # Guards the wiring rather than the context manager: _drive must enter the manager and keep a
    # handle, because the restore happens in a finally far from the patch site.
    import inspect

    src = inspect.getsource(gs._run_events)
    assert "gateway_patch = dry_run_gateway()" in src
    assert "gateway_patch.__exit__(None, None, None)" in src
