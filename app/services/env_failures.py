"""Is a failing check the CODE's fault, or the ENVIRONMENT's? (deterministic, no LLM)

The Debugging agent can only fix code. Handing it a failure the code cannot cause — a wheel that
needs a C compiler this machine lacks, a registry that cannot be reached, a missing toolchain —
burns its whole round budget on edits that cannot help, then escalates, which skips docs,
security, the pull request and the zip. In a real run ``asyncpg`` failed to build on the tool's
Python 3.14 behind every TypeScript error, and nothing could have fixed it from the code.

:func:`environment_reason` names the environment problem in one failing tool's output, or returns
``""`` when the failure is (or may be) the code's. Conservative by construction: only signatures
that a code edit cannot produce are matched, so an ambiguous failure stays a code failure and still
reaches the debugging loop. A timeout counts only for installers — a test or a compiler that hangs
may well be an infinite loop in the generated code.

Narrower on purpose than ``check_digest.environment_signals``, which DESCRIBES possible causes to
the Debugging agent (a refused connection, a timeout) — those can be the code's fault, and a
description can afford to be wrong where a routing decision cannot.
"""

from __future__ import annotations

import re

#: (reason, pattern) — first match wins. Patterns are matched against stdout + stderr together.
_SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a package needs a C/C++ build toolchain that is not installed",
     re.compile(r"Microsoft Visual C\+\+ [\d.]+ or greater is required|error: Unable to find vcvarsall\.bat|"
                r"\bgcc\b[^\n]*(?:not found|No such file)|command 'gcc' failed|cl\.exe['\"]? (?:failed|not found)",
                re.IGNORECASE)),
    ("a package has no build for this Python version and could not be compiled",
     re.compile(r"Failed (?:to )?build(?:ing)? (?:wheel for|installable wheels for)|Could not build wheels for|"
                r"error: metadata-generation-failed|Failed to build `?[\w.-]+`?|"
                r"No matching distribution found for [^\n]*\(from versions: none\)|"
                r"requires a different Python|Requires-Python [^\n]*(?:not|incompatible)",
                re.IGNORECASE)),
    ("the package registry or network could not be reached",
     re.compile(r"Temporary failure in name resolution|NameResolutionError|getaddrinfo (?:failed|ENOTFOUND|EAI_AGAIN)|\bEAI_AGAIN\b|"
                r"Could not resolve host|Network is unreachable|Max retries exceeded with url|"
                r"\bnpm ERR! (?:code )?(?:ETIMEDOUT|ECONNRESET|ECONNREFUSED|ENOTFOUND)\b|"
                r"request to https?://registry\.[^\s]+ failed|Failed to fetch[^\n]*(?:pypi|registry)|"
                r"error sending request for url \(https?://(?:pypi|files\.pythonhosted)",
                re.IGNORECASE)),
    ("a required tool is not installed on this machine",
     re.compile(r"command not found: |is not recognized as an internal or external command|"
                r"No module named pip\b|\bnpm: (?:command )?not found|\bnode: (?:command )?not found|"
                r"\bdocker: (?:command )?not found|Cannot connect to the Docker daemon",
                re.IGNORECASE)),
    ("the disk is full",
     re.compile(r"\bENOSPC\b|No space left on device", re.IGNORECASE)),
    ("a path on this machine is too long (Windows path-length limit)",
     re.compile(r"Trivial strip failed|The filename or extension is too long|path too long|ENAMETOOLONG|"
                r"\[WinError 206\]", re.IGNORECASE)),
    ("a file was locked by another process (Windows)",
     re.compile(r"\bnpm ERR! code (?:EPERM|EBUSY)\b|EBUSY: resource busy or locked|"
                r"The process cannot access the file because it is being used by another process",
                re.IGNORECASE)),
    ("the throwaway database could not be started",
     re.compile(r"\[database unavailable\]", re.IGNORECASE)),
)

#: Tool labels whose TIMEOUT is the environment's (slow registry, cold cache), never the code's.
_INSTALLER_LABEL_RE = re.compile(r"^(?:pip|npm-install|uv|venv(?:-[\d.]+)?|install)\b", re.IGNORECASE)


def environment_reason(label: str, output: str, *, timed_out: bool = False, exit_code: int = 0) -> str:
    """Why this failure is the environment's, or ``""`` if it may be the code's."""
    for reason, pattern in _SIGNATURES:
        if pattern.search(output or ""):
            return reason
    if exit_code == 127 and not (output or "").strip().startswith("Traceback"):
        return "a required tool is not installed on this machine"
    if timed_out and _INSTALLER_LABEL_RE.match(label or ""):
        return "installing dependencies timed out"
    return ""
