"""Environment failures are "unverified", never work for the Debugging agent — and the boot check.

A failure the code cannot cause (a wheel needing a compiler this machine lacks, an unreachable
registry, a missing toolchain) used to be handed to the Debugging agent like any other: it burned
every round editing code that could not help, then escalated — and escalation is terminal, so the
run lost its docs, security scan, pull request and zip over a missing C compiler. Now such a check
is INCONCLUSIVE: the run carries on and records what it could not verify.
"""

from __future__ import annotations

from app.agents.debugging import _failing_checks
from app.graph.nodes import debug_check_node, unit_test_run_node
from app.graph.router import route_after_debug_check, route_after_test_run
from app.graph.state import new_state
from app.integrations.executor import CheckResult, Executor, FakeExecutor, RunResult, aggregate_check_results, set_executor
from app.services.env_failures import environment_reason

PIP_MSVC = ("Building wheel for asyncpg (pyproject.toml) ... error\n"
            "error: Microsoft Visual C++ 14.0 or greater is required. Get it with \"Microsoft C++ Build Tools\"")
TSC = "src/a.ts(1,1): error TS2304: Cannot find name 'x'."


def _run(out: str = "", err: str = "", code: int = 1, timed_out: bool = False) -> RunResult:
    return RunResult(stdout=out, stderr=err, exit_code=code, timed_out=timed_out)


# --------------------------------------------------------------- classification

def test_environment_signatures_are_recognised():
    assert "C/C++ build toolchain" in environment_reason("pip", PIP_MSVC)
    assert "no build for this Python" in environment_reason("pip", "ERROR: Failed building wheel for asyncpg")
    assert "network" in environment_reason("npm-install", "npm ERR! code ENOTFOUND\nnpm ERR! request to https://registry.npmjs.org/x failed")
    assert "not installed" in environment_reason("tsc", "'npx' is not recognized as an internal or external command")
    assert "too long" in environment_reason("pip", "error: Failed to install: x.whl\n  Caused by: Trivial strip failed")
    assert "database" in environment_reason("boot", "[database unavailable] Docker is not available")


def test_code_failures_are_never_called_environmental():
    for label, text in (("tsc", TSC), ("pytest", "E   AssertionError: assert 1 == 2"),
                        ("boot", "ModuleNotFoundError: No module named 'app.routers'"),
                        ("py", "SyntaxError: invalid syntax")):
        assert environment_reason(label, text) == "", label


def test_only_an_installer_timeout_is_environmental():
    assert environment_reason("npm-install", "[timed out]", timed_out=True) == "installing dependencies timed out"
    assert environment_reason("pytest", "[timed out]", timed_out=True) == ""      # may be an infinite loop


# --------------------------------------------------------------- aggregation

def test_a_check_whose_every_failure_is_environmental_is_inconclusive():
    result = aggregate_check_results("build", [("pip[p/be]", _run(err=PIP_MSVC), "p/be"), ("npm[p/fe]", _run(code=0), "p/fe")])
    assert not result.passed and result.inconclusive
    assert result.environment.startswith("pip[p/be]: a package needs a C/C++ build toolchain")


def test_mixed_failures_keep_every_block_annotate_the_environment_and_stay_actionable():
    result = aggregate_check_results("build", [("pip[p/be]", _run(err=PIP_MSVC), "p/be"),
                                               ("tsc[p/fe]", _run(out=TSC), "p/fe")])
    assert not result.passed and not result.inconclusive
    assert result.stderr.startswith("[pip[p/be]] " + PIP_MSVC)                    # still visible
    assert "(environment: a package needs a C/C++ build toolchain" in result.stderr
    assert "[tsc[p/fe]] " + TSC in result.stderr
    assert result.stderr.rstrip().endswith("- pip[p/be]: a package needs a C/C++ build toolchain that is not installed")
    assert result.scope == "p/fe"                                                 # the code failure leads


def test_an_executor_that_cannot_start_a_server_says_so():
    class Bare(FakeExecutor):
        boot = Executor.boot
    result = Bare().boot("p")
    assert result.inconclusive and not result.passed and "cannot start" in result.environment


# --------------------------------------------------------------- the debug check

def _debug(executor: FakeExecutor, **state_extra) -> dict:
    state = new_state(run_id="r", attempt=1, project_id="p", design_package={})
    state.update(state_extra)
    set_executor(executor)
    try:
        return debug_check_node(state)
    finally:
        set_executor(None)


def test_boot_runs_after_compile_and_build_pass():
    out = _debug(FakeExecutor(boot_results=[CheckResult(name="boot", passed=False, stderr="[boot[p/be]] ImportError")]))
    assert [c["name"] for c in out["debug_result"]["checks"]] == ["compile", "build", "boot"]
    assert not out["debug_result"]["passed"] and not out["debug_result"]["inconclusive"]
    assert route_after_debug_check(out) == "debugging"


def test_a_frontend_type_error_does_not_hold_back_the_backend_boot():
    tsc = CheckResult(name="compile", passed=False, stderr="[tsc[p/fe]] " + TSC)
    out = _debug(FakeExecutor(compile_results=[tsc]))
    assert [c["name"] for c in out["debug_result"]["checks"]] == ["compile", "build", "boot"]


def test_boot_waits_while_a_backend_install_fails():
    pip = CheckResult(name="build", passed=False, stderr="[pip[p/be]] ERROR: No matching distribution found for nope==9")
    out = _debug(FakeExecutor(build_results=[pip]))
    assert [c["name"] for c in out["debug_result"]["checks"]] == ["compile", "build"]


def test_an_environment_only_failure_continues_unverified():
    env = CheckResult(name="build", passed=False, inconclusive=True, stderr="[pip[p/be]] " + PIP_MSVC,
                      environment="pip[p/be]: a package needs a C/C++ build toolchain that is not installed")
    out = _debug(FakeExecutor(build_results=[env]))
    result = out["debug_result"]
    assert not result["passed"] and result["inconclusive"]
    assert result["checks"][-1]["name"] == "boot" and result["checks"][-1]["inconclusive"]   # never attempted
    assert route_after_debug_check(out) == "unit_test_generate"
    assert {u["check"] for u in out["unverified_checks"]} == {"build", "boot"}
    assert _failing_checks(out) == ("compile/build", [])                         # nothing for the agent


def test_a_check_that_later_passes_is_no_longer_unverified():
    prior = [{"check": "build", "reason": "pip: no compiler"}, {"check": "boot", "reason": "not attempted"}]
    out = _debug(FakeExecutor(), unverified_checks=prior)
    assert out["unverified_checks"] == [] and out["debug_result"]["passed"]


def test_the_debugging_agent_sees_only_the_code_failures():
    state = {"debug_result": {"passed": False, "checks": [
        {"name": "compile", "passed": False, "stderr": TSC},
        {"name": "build", "passed": False, "inconclusive": True, "stderr": PIP_MSVC},
    ]}}
    kind, failing = _failing_checks(state)
    assert kind == "compile/build" and [c["name"] for c in failing] == ["compile"]


def test_an_inconclusive_test_run_finishes_the_loop():
    state = new_state(run_id="r", attempt=1, project_id="p", design_package={})
    env = CheckResult(name="test", passed=False, inconclusive=True, stderr="[pytest[p/be]] x", environment="pytest: x")
    set_executor(FakeExecutor(test_results=[env]))
    try:
        out = unit_test_run_node(state)
    finally:
        set_executor(None)
    assert out["test_result"]["inconclusive"] and route_after_test_run(out) == "done"
    assert out["unverified_checks"] == [{"check": "test", "reason": "pytest: x"}]


def test_a_real_test_failure_still_goes_to_debugging():
    state = {"test_result": {"passed": False, "checks": [{"name": "test", "passed": False}]}, "debug_attempt": 0}
    assert route_after_test_run(state) == "debugging"
