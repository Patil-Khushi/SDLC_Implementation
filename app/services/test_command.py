"""Find and run the target repository's OWN test suite — deterministic, no LLM.

``Executor.test()`` was written for the projects this service generates, and it gates pytest on
``requirements.txt`` and npm on ``package.json``. A repository we did not write frequently matches
neither: anything packaged with ``pyproject.toml`` (most Python projects since ~2021) falls through
every branch and ``test()`` returns ``passed=True`` having run nothing. For greenfield that is a
harmless no-op — the pipeline wrote the project and knows its shape. For brownfield it is a
**vacuous green**, and a vacuous green is worse than no check at all, because the run reports that
the change was verified when nothing was executed.

So this module answers the question directly: what does THIS repository run to test itself? It
reads the repository's own manifests for evidence and reports what it found, and when it finds
nothing it says so rather than substituting a guess.

The three-way outcome is the point. ``inconclusive`` is a first-class result, distinct from
``failed``:

* **failed** — the suite ran and tests did not pass. Actionable.
* **inconclusive** — the suite could not run at all (missing dev dependency, import error, no tests
  collected, tool not installed, timeout). NOT a failure of the change.

Collapsing those two would be the worst possible error here: a repo whose dev dependencies are not
installed would report "your change broke the tests", blocking a correct change and sending the
model off to "fix" code that was never broken.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass, field

from app.integrations.executor import Executor

logger = logging.getLogger(__name__)

#: How long a target repo's suite may run. Long enough for a real project, short enough that a
#: hung suite does not hold a run open indefinitely — a timeout is INCONCLUSIVE, never a failure.
TEST_TIMEOUT_SECONDS = 600.0

#: Cap on captured output kept for the report.
MAX_OUTPUT_CHARS = 4_000

#: npm's placeholder from `npm init`. A repo carrying it has no test script, whatever the key says.
_NPM_PLACEHOLDER = "no test specified"

#: The interpreter's own startup error for `python -m <missing>`. Anchored to an
#: interpreter path so an ImportError raised INSIDE a test (a legitimate failure) is not
#: mistaken for the runner being absent.
_RUNNER_MISSING_RE = re.compile(
    r"^.*python(?:\.exe)?[\"']?\s*:\s*No module named\s+\S+",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class TestCommand:
    """A concrete way to run this repository's tests, and the evidence for choosing it."""

    # pytest tries to collect any class named Test*; this is a dataclass, not a test case.
    __test__ = False

    argv: list[str]
    label: str                                  # "pytest", "npm test", ... (for the report)
    evidence: str                               # WHICH file justified this choice
    cwd: str = ""                               # project-relative; "" = the repo root
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TestOutcome:
    """What happened. ``status`` is the only thing callers should branch on."""

    __test__ = False                            # see TestCommand

    status: str                                 # "passed" | "failed" | "inconclusive"
    summary: str                                # one line, for the report and the PR
    label: str = ""
    output: str = ""

    @property
    def conclusive(self) -> bool:
        return self.status in ("passed", "failed")

    @property
    def passed(self) -> bool:
        return self.status == "passed"


def _read(executor: Executor, project_dir: str, rel: str) -> str | None:
    try:
        return executor.read_file(f"{project_dir}/{rel}")
    except Exception:  # noqa: BLE001 - absent is a normal answer here
        return None


def _has_python_tests(files: list[str]) -> bool:
    return any(
        re.search(r"(^|/)(tests?)/", p) or re.search(r"(^|/)test_[^/]+\.py$", p)
        or re.search(r"_test\.py$", p)
        for p in files
    )


def detect(executor: Executor, project_dir: str, inventory: dict) -> TestCommand | None:
    """The command this repository uses to test itself, or ``None`` when there is no evidence.

    Ordered by how specific the evidence is. Returning ``None`` is a legitimate outcome — some
    repositories genuinely have no runnable suite — and the caller reports that rather than
    pretending a check ran.
    """
    files = list(inventory.get("files") or [])
    manifests = dict(inventory.get("manifests") or {})

    # 1. An explicit npm test script. The most direct statement a repo can make about its tests.
    for manifest_path in sorted(p for p in manifests if p.endswith("package.json")):
        raw = _read(executor, project_dir, manifest_path)
        if not raw:
            continue
        try:
            script = ((json.loads(raw).get("scripts") or {}).get("test") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            continue
        if script and _NPM_PLACEHOLDER not in script.lower():
            cwd = manifest_path.rsplit("/", 1)[0] if "/" in manifest_path else ""
            return TestCommand(argv=["npm", "test", "--silent"], label="npm test",
                               evidence=f"{manifest_path} scripts.test", cwd=cwd)

    # 2. Python. pytest is the near-universal runner; the evidence is any config that mentions it,
    #    or simply the presence of test files.
    py_config = next(
        (name for name in ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini")
         if (raw := _read(executor, project_dir, name)) is not None and "pytest" in raw),
        None,
    )
    if py_config or (_has_python_tests(files) and any(
        m in ("pip",) for m in manifests.values()
    )) or (_has_python_tests(files) and inventory.get("primary_language") == ".py"):
        env = {}
        # A src/ layout puts the package somewhere pytest will not find without help, and an
        # uninstalled package is the single most common reason a suite looks "broken" when it is
        # merely un-importable. Point PYTHONPATH at it rather than requiring an editable install.
        if any(p.startswith("src/") for p in files):
            env["PYTHONPATH"] = "src"
        return TestCommand(
            # sys.executable, NOT a bare "python": on PATH that resolves to whatever interpreter
            # comes first, which is routinely a base install without pytest — and `python -m
            # pytest` then exits 1 ("No module named pytest"), the same code a real test failure
            # uses. Observed live: the run reported "the tests failed" when nothing had run.
            # LocalDiskExecutor.test() already uses sys.executable for this reason.
            argv=[sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
            label="pytest", evidence=py_config or "test files present", env=env,
        )

    if "go.mod" in files:
        return TestCommand(argv=["go", "test", "./..."], label="go test", evidence="go.mod")
    if "Cargo.toml" in files:
        return TestCommand(argv=["cargo", "test"], label="cargo test", evidence="Cargo.toml")

    makefile = _read(executor, project_dir, "Makefile")
    if makefile and re.search(r"^test\s*:", makefile, re.MULTILINE):
        return TestCommand(argv=["make", "test"], label="make test", evidence="Makefile test target")
    return None


def _classify(command: TestCommand, result) -> TestOutcome:
    """Map a runner's exit code onto passed / failed / inconclusive.

    The distinction that matters: an exit code meaning "I could not run" must never be reported as
    "your tests failed". pytest is explicit about this — 0 passed, 1 tests failed, and 2/3/4/5 all
    mean the run itself did not happen properly (interrupted, internal error, bad usage, nothing
    collected).
    """
    output = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-MAX_OUTPUT_CHARS:]
    code = result.exit_code

    if getattr(result, "timed_out", False):
        return TestOutcome("inconclusive", f"{command.label} timed out", command.label, output)
    if code == 127 or "command not found" in output.lower() or "is not recognized" in output.lower():
        return TestOutcome("inconclusive", f"{command.label} is not installed", command.label, output)
    # `python -m <missing>` exits 1 — the SAME code a real test failure uses — after printing this
    # from the interpreter itself. Checked before the exit-code table because the code alone cannot
    # distinguish "the runner is absent" from "the tests failed", and calling the first one a
    # failure is precisely the misreport this module exists to avoid.
    if _RUNNER_MISSING_RE.search(output):
        return TestOutcome("inconclusive", f"{command.label} is not installed in the interpreter "
                                           "used to run it", command.label, output)

    if command.label == "pytest":
        if code == 0:
            return TestOutcome("passed", _pytest_summary(output) or "pytest passed", "pytest", output)
        if code == 1:
            return TestOutcome("failed", _pytest_summary(output) or "pytest reported failures",
                               "pytest", output)
        if code == 5:
            return TestOutcome("inconclusive", "pytest collected no tests", "pytest", output)
        return TestOutcome("inconclusive", f"pytest could not run (exit {code})", "pytest", output)

    if code == 0:
        return TestOutcome("passed", f"{command.label} passed", command.label, output)
    # A non-pytest runner that fails to even start usually says so; treat an import/collection
    # style error as inconclusive rather than blaming the change.
    if re.search(r"(cannot find module|modulenotfounderror|no such file|missing script)", output, re.I):
        return TestOutcome("inconclusive", f"{command.label} could not run", command.label, output)
    return TestOutcome("failed", f"{command.label} reported failures", command.label, output)


def _pytest_summary(output: str) -> str:
    """pytest's own last summary line ("3 failed, 69 passed in 0.15s"), when it printed one."""
    match = re.findall(r"^[=\s]*(\d+ (?:failed|passed)[^\n]*)$", output, re.MULTILINE)
    return match[-1].strip("= ") if match else ""


def run(executor: Executor, project_dir: str, command: TestCommand) -> TestOutcome:
    """Run ``command`` in the repository. Never raises — an exception is inconclusive, not failure."""
    cwd = f"{project_dir}/{command.cwd}".rstrip("/")
    try:
        result = executor.run_command(
            command.argv, cwd=cwd, timeout=TEST_TIMEOUT_SECONDS, env=_env_for(command),
        )
    except Exception as exc:  # noqa: BLE001 - could not run == inconclusive
        logger.warning("[tests] %s could not be run: %s", command.label, exc)
        return TestOutcome("inconclusive", f"{command.label} could not be run: {exc}", command.label)
    outcome = _classify(command, result)
    logger.info("[tests] %s -> %s (%s)", command.label, outcome.status, outcome.summary)
    return outcome


def _env_for(command: TestCommand) -> dict[str, str] | None:
    """Layer the command's own variables on top of the real environment.

    ``env=`` REPLACES a subprocess environment rather than extending it, so a bare dict would strip
    PATH and the interpreter would not even be found.
    """
    if not command.env:
        return None
    import os

    return {**os.environ, **command.env}
