"""What a failed compile/build/test check is actually saying — deterministic, no LLM, no I/O.

The debug/test loop used to hand the Debugging agent the raw output of the FIRST failing tool and
measure progress as "how many checks failed". Both were too coarse for a generated project:

* A 209-error ``tsc`` run and a 1-error one both count as "1 failing check", so a round that fixed
  half the errors looked exactly like a round that fixed nothing, and the stall cap killed a loop
  that was converging.
* The model was told *that* things failed but had to dig *where* out of 40K characters of output,
  and a second failing tool (``pip`` dying on a native build) never reached it at all.

This module turns check output into three facts the agent and the loop can use: a quantified
error count, errors grouped by file (so a round can fix the worst files first), and signals that a
failure is ENVIRONMENTAL — a missing compiler, no network, a timeout — which no edit to the
generated code can fix. Everything here is a pure function over text and degrades to "nothing
recognised" rather than guessing.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

#: ``[label]`` / ``[label[dir]]`` prefix the executors put on each failing tool's output (see
#: ``app.integrations.executor.aggregate_check_results``), e.g. ``[tsc[feet/frontend]] ...``.
_TOOL_BLOCK_RE = re.compile(r"^\[(?P<label>[\w.-]+)(?:\[(?P<dir>[^\]]*)\])?\] ?", re.MULTILINE)

# --- quantified counts, most specific first -------------------------------------------------
_TSC_FOUND_RE = re.compile(r"Found (\d+) errors?\b")
_JEST_TESTS_RE = re.compile(r"^Tests:\s+(\d+)\s+failed", re.MULTILINE)
_JEST_SUITES_RE = re.compile(r"^Test Suites:\s+(\d+)\s+failed", re.MULTILINE)
_VITEST_TESTS_RE = re.compile(r"^\s*Tests\s+(\d+)\s+failed", re.MULTILINE)
# pytest -q prints "3 failed, 10 passed in 0.52s" (with or without ===== framing), and collection
# problems as "2 errors in 0.1s". Anchored to a line that ends in the timing so a "5 failed"
# quoted inside a traceback or snapshot can't be mistaken for the summary.
_PYTEST_SUMMARY_RE = re.compile(r"^[= ]*(?P<body>[^\n]*\b(?:failed|errors?|passed)\b[^\n]*) in [\d.]+s\b", re.MULTILINE)
_PYTEST_FAILED_RE = re.compile(r"(\d+) failed")
_PYTEST_ERRORS_RE = re.compile(r"(\d+) errors?\b")
_BARE_FAILED_RE = re.compile(r"^\s*(\d+)\s+failed", re.MULTILINE)

# --- errors attributed to a file ----------------------------------------------------------------
_SOURCE_EXT = r"(?:tsx?|jsx?|mts|cts|mjs|cjs|vue|py)"
_FILE_PATTERNS = (
    # tsc, non-pretty: src/pages/Login.tsx(12,5): error TS2322: ...
    re.compile(rf"^(?P<path>[^\s(\[][^(\n]*?\.{_SOURCE_EXT})\(\d+,\d+\): error\b", re.MULTILINE),
    # tsc --pretty / esbuild / eslint-unix / ruff / mypy: path:12:5 - error TS... | path:12:5: error
    re.compile(rf"^(?P<path>[^\s:\[][^:\n]*?\.{_SOURCE_EXT}):\d+:\d+(?: - |: )(?:error|E\d)", re.MULTILINE),
    # pytest short summary: FAILED tests/test_x.py::test_y - ... | ERROR tests/test_x.py
    re.compile(rf"^(?:FAILED|ERROR) (?P<path>[^\s:]+\.{_SOURCE_EXT})\b", re.MULTILINE),
    # compileall: *** Error compiling './backend/app/x.py'...
    re.compile(rf"Error compiling '(?P<path>[^']+\.{_SOURCE_EXT})'"),
    # vite/rollup: Could not resolve "./x" from "src/App.tsx"
    re.compile(rf'Could not resolve "[^"]+" from "(?P<path>[^"]+\.{_SOURCE_EXT})"'),
    # Python traceback frame: File "C:\\...\\backend\\app\\main.py", line 24
    re.compile(rf'File "(?P<path>[^"]+\.{_SOURCE_EXT})", line \d+'),
)

#: Path fragments that mark a frame/file as NOT the generated project's own code.
_THIRD_PARTY_MARKERS = (
    "site-packages", "dist-packages", "/lib/python", "node_modules", ".py_packages", "<frozen",
    "/python3.", "/cpython-", "/Lib/",
)

# --- failures no code edit can fix -----------------------------------------------------------
_ENVIRONMENT_SIGNALS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Microsoft Visual C\+\+ [\d.]+ or greater is required"),
     "a Python dependency has no pre-built wheel for this Python version and needs a C compiler to build"),
    (re.compile(r"Failed building wheel for (?P<x>[\w.-]+)"),
     "pip could not build {x} from source"),
    (re.compile(r"No matching distribution found for (?P<x>[^\s]+)"),
     "pip found no installable version of {x} for this Python"),
    (re.compile(r"NameResolutionError|getaddrinfo (?:failed|ENOTFOUND)|ENOTFOUND|EAI_AGAIN|"
                r"Temporary failure in name resolution|Could not resolve host"),
     "no network / DNS: a package registry could not be reached"),
    (re.compile(r"\[timed out\]"),
     "a command timed out before finishing"),
    (re.compile(r"is not recognized as an internal or external command|: command not found|"
                r"No such file or directory: '(?:npm|npx|node|python3?)'"),
     "a required tool (npm/node/python) is not installed on the build host"),
    (re.compile(r"ECONNREFUSED|could not connect to server|Connection refused|"
                r"connection to server at .* failed"),
     "a service the code connects to (e.g. the database) is not running"),
)


@dataclass(frozen=True)
class ToolBlock:
    """One failing tool's output inside a check, with the directory it ran in (if labelled)."""

    label: str
    directory: str
    body: str


@dataclass
class CheckDigest:
    """Everything recognised in the output of the currently failing check(s)."""

    error_count: int = 0                       # quantified when a tool printed a count, else #failing tools
    quantified: bool = False                   # True when error_count came from a tool's own summary
    errors_by_file: Counter[str] = field(default_factory=Counter)
    environment: list[str] = field(default_factory=list)

    def top_files(self, limit: int = 25) -> list[tuple[str, int]]:
        return self.errors_by_file.most_common(limit)


def split_tool_blocks(text: str) -> list[ToolBlock]:
    """Split aggregated check output back into its per-tool blocks.

    Output without any ``[label]`` prefix (a hand-built CheckResult, an older executor) comes back
    as ONE unlabelled block, so callers never have to special-case it.
    """
    matches = list(_TOOL_BLOCK_RE.finditer(text or ""))
    if not matches:
        return [ToolBlock(label="", directory="", body=text or "")] if (text or "").strip() else []
    blocks: list[ToolBlock] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append(ToolBlock(label=m.group("label"), directory=(m.group("dir") or "").strip("/"),
                                body=text[m.end():end]))
    return blocks


def error_count(text: str) -> int | None:
    """The number of errors/failures a tool itself reported, or ``None`` if it printed no count.

    ``tsc`` summaries are SUMMED (one per frontend directory); otherwise the most specific runner
    summary wins: jest test count, jest suite count, vitest, pytest's ``N failed, M errors``.
    """
    if not text:
        return None
    tsc = [int(n) for n in _TSC_FOUND_RE.findall(text)]
    if tsc:
        return sum(tsc)
    for pattern in (_JEST_TESTS_RE, _JEST_SUITES_RE, _VITEST_TESTS_RE):
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    summary = _PYTEST_SUMMARY_RE.search(text)
    if summary:
        body = summary.group("body")
        failed = sum(int(n) for n in _PYTEST_FAILED_RE.findall(body))
        errors = sum(int(n) for n in _PYTEST_ERRORS_RE.findall(body))
        if failed or errors:
            return failed + errors
    match = _BARE_FAILED_RE.search(text)
    if match:
        return int(match.group(1))
    return None


def _normalize_path(raw: str, directory: str, project_dir: str) -> str | None:
    """Map a path as a tool printed it to a project-relative path, or ``None`` for third-party code.

    Tools print paths relative to where they ran (``tsc`` in ``feet/frontend`` prints
    ``src/App.tsx``), absolute (Python tracebacks), or ``./``-prefixed (compileall); all three are
    brought to the same ``<side>/<path>`` form the agent's ``read_file`` accepts.
    """
    path = raw.strip().replace("\\", "/")
    if any(marker in path for marker in _THIRD_PARTY_MARKERS):
        return None
    while path.startswith("./"):
        path = path[2:]
    prefix = f"{project_dir}/" if project_dir else ""
    if prefix:
        anchor = f"/{prefix}"
        if anchor in path:                       # absolute path that runs through the project dir
            path = path[path.rindex(anchor) + len(anchor):]
        elif path.startswith(prefix):
            path = path[len(prefix):]
    if re.match(r"^[A-Za-z]:/|^/", path):        # still absolute: not inside this project
        return None
    if directory:
        side = directory[len(prefix):] if prefix and directory.startswith(prefix) else directory
        side = side.strip("/")
        if side and side != "." and not path.startswith(f"{side}/"):
            path = f"{side}/{path}"
    return path or None


def _block_file_errors(block: ToolBlock, project_dir: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for pattern in _FILE_PATTERNS:
        for match in pattern.finditer(block.body):
            path = _normalize_path(match.group("path"), block.directory, project_dir)
            if path:
                counts[path] += 1
    return counts


def errors_by_file(text: str, *, project_dir: str = "") -> Counter[str]:
    """``project-relative path -> error count`` for every error line attributable to a file."""
    counts: Counter[str] = Counter()
    for block in split_tool_blocks(text):
        counts.update(_block_file_errors(block, project_dir))
    return counts


def environment_signals(text: str) -> list[str]:
    """Plain-language reasons a failure is environmental (no code edit can fix it). Deduped, ordered."""
    found: list[str] = []
    for pattern, message in _ENVIRONMENT_SIGNALS:
        templated = "{x}" in message
        for match in pattern.finditer(text or ""):
            line = message.format(x=match.group("x")) if templated else message
            if line not in found:
                found.append(line)
            if not templated:
                break  # an untemplated signal reads the same however often it occurs
    return found


def digest_checks(checks: list[dict], *, project_dir: str = "") -> CheckDigest:
    """Digest every FAILING check of a gate result (plain ``GateCheck`` dicts from state)."""
    digest = CheckDigest()
    failing = [c for c in checks if not c.get("passed", True)]
    any_quantified = False
    total = 0
    for check in failing:
        text = check_output(check)
        # Counted PER TOOL BLOCK: a tool that printed its own count contributes that count; one that
        # printed none contributes its number of file-attributed error lines (non-interactive
        # `tsc` prints NO "Found N errors" summary, only the error lines themselves); one that
        # failed with neither contributes 1. So "tsc reported 20 errors AND pip failed" measures
        # as 21, and fixing either side shows up as progress.
        for block in split_tool_blocks(text) or [ToolBlock("", "", text)]:
            per_file = _block_file_errors(block, project_dir)
            digest.errors_by_file.update(per_file)
            count = error_count(block.body)
            if count is None and per_file:
                count = sum(per_file.values())
            if count is not None:
                any_quantified = True
                total += count
            else:
                total += 1
        for signal in environment_signals(text):
            if signal not in digest.environment:
                digest.environment.append(signal)
    digest.error_count = total
    digest.quantified = any_quantified
    return digest


def check_output(check: dict) -> str:
    """A check's diagnostic text with stdout included only when stderr does not already carry it.

    The executors fold stdout into stderr (pytest and tsc print their diagnostics to stdout) AND
    keep stdout separately, so naively printing both doubles every prompt's output section.
    """
    stderr = str(check.get("stderr") or "")
    stdout = str(check.get("stdout") or "")
    if not stdout.strip() or stdout.strip() in stderr:
        return stderr
    if not stderr.strip():
        return stdout
    return f"{stderr}\n{stdout}"
