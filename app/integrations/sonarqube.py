"""SonarQube integration for the Code Review agent.

Pulls open issues for a project from a SonarQube server's Web API and returns them normalized as
review findings. Per DEVELOPER_GUIDE.md rule 6 (outside tools live in ``integrations/``), the
agent calls this wrapper — it never talks to SonarQube directly.

SonarQube is a network service, not a sandbox command, so this integration uses ``httpx`` from
the service host (like ``integrations/github.py``) rather than the exec-sandbox Executor. The
sandbox has no egress to it anyway (CLAUDE.md rule 6).

Design notes:
- The scanner is OFF unless configured; a disabled/unconfigured client, an HTTP error, or a
  malformed response all degrade to an EMPTY finding list plus a diagnostic — the review still
  runs. Static analysis must never crash the pipeline.
- ``http_get`` is injectable so tests exercise the parsing without a live server or the network.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.config.settings import get_settings

#: A minimal seam over the HTTP layer: (url, params, headers, timeout) -> parsed JSON dict.
HttpGet = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]


@dataclass(frozen=True)
class SonarIssue:
    """One normalized SonarQube issue (maps onto a review-report finding)."""

    key: str
    path: str
    line: int
    severity: str          # normalized to "high" | "medium" | "low"
    message: str
    rule: str
    type: str = ""         # raw SonarQube type: BUG | VULNERABILITY | CODE_SMELL (for categorization)


@dataclass(frozen=True)
class SonarResult:
    """Outcome of a fetch: the issues plus an optional diagnostic when it degraded."""

    issues: list[SonarIssue] = field(default_factory=list)
    error: str = ""        # empty on a clean run; a human-readable reason when degraded/skipped

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass(frozen=True)
class SonarMeasures:
    """Project metrics MEASURED by SonarQube (deterministic - the LLM never computes these)."""

    values: dict[str, str] = field(default_factory=dict)   # metricKey -> raw value string
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


# Engineering metrics pulled from SonarQube's measures API (all tool-computed, never estimated).
MEASURE_KEYS = [
    "ncloc", "complexity", "cognitive_complexity", "coverage",
    "duplicated_lines_density", "sqale_index", "bugs", "vulnerabilities",
    "code_smells", "security_hotspots",
]


# SonarQube severities → the review report's three-level scale.
_SEVERITY_MAP = {
    "BLOCKER": "high",
    "CRITICAL": "high",
    "MAJOR": "medium",
    "MINOR": "low",
    "INFO": "low",
}


class SonarQubeClient:
    """Fetches open issues for one project component from a SonarQube server."""

    def __init__(
        self,
        *,
        enabled: bool,
        base_url: str = "",
        token: str = "",
        project_key: str = "",
        timeout: float = 30.0,
        http_get: HttpGet | None = None,
    ) -> None:
        self._enabled = enabled
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._project_key = project_key
        self._timeout = timeout
        self._http_get = http_get  # injected in tests; real HTTP built lazily otherwise

    def fetch_issues(self) -> SonarResult:
        """Return open issues for the configured project (never raises)."""
        if not self._enabled:
            return SonarResult(error="sonarqube disabled (SONARQUBE_ENABLED=false)")
        if not (self._base_url and self._project_key):
            return SonarResult(error="sonarqube not configured (url/project_key missing)")

        url = f"{self._base_url}/api/issues/search"
        params = {"componentKeys": self._project_key, "statuses": "OPEN,CONFIRMED,REOPENED", "ps": 500}
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            payload = self._get(url, params, headers, self._timeout)
        except Exception as exc:  # noqa: BLE001 - a scanner failure must not crash the review
            return SonarResult(error=f"sonarqube request failed: {exc}")
        return self._parse(payload)

    def fetch_measures(self) -> SonarMeasures:
        """Return tool-computed project metrics (LOC, complexity, coverage, duplication, tech debt).

        Deterministic: these come straight from SonarQube's Compute Engine, never estimated. Never
        raises - degrades to an empty measure set with a diagnostic.
        """
        if not self._enabled:
            return SonarMeasures(error="sonarqube disabled")
        if not (self._base_url and self._project_key):
            return SonarMeasures(error="sonarqube not configured")
        url = f"{self._base_url}/api/measures/component"
        params = {"component": self._project_key, "metricKeys": ",".join(MEASURE_KEYS)}
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            payload = self._get(url, params, headers, self._timeout)
        except Exception as exc:  # noqa: BLE001
            return SonarMeasures(error=f"sonarqube measures request failed: {exc}")
        if not isinstance(payload, dict):
            return SonarMeasures(error="sonarqube measures response was not a JSON object")
        component = payload.get("component") or {}
        values = {
            str(m.get("metric")): str(m.get("value", ""))
            for m in component.get("measures", []) or []
            if isinstance(m, dict) and m.get("metric")
        }
        return SonarMeasures(values=values)

    def _get(self, url: str, params: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
        if self._http_get is not None:
            return self._http_get(url, params, headers, timeout)
        import httpx  # lazy: keep module import free of a hard httpx dependency at import time

        response = httpx.get(url, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _parse(payload: Any) -> SonarResult:
        if not isinstance(payload, dict):
            return SonarResult(error="sonarqube response was not a JSON object")
        issues: list[SonarIssue] = []
        for issue in payload.get("issues", []) or []:
            if not isinstance(issue, dict):
                continue
            component = str(issue.get("component", ""))
            # component is "<projectKey>:<path>"; keep the path part when present.
            path = component.split(":", 1)[1] if ":" in component else component
            raw_severity = str(issue.get("severity", "INFO")).upper()
            issues.append(
                SonarIssue(
                    key=str(issue.get("key", "")),
                    path=path,
                    line=int(issue.get("line", 0) or 0),
                    severity=_SEVERITY_MAP.get(raw_severity, "low"),
                    message=str(issue.get("message", "")).strip(),
                    rule=str(issue.get("rule", "")),
                    type=str(issue.get("type", "")).upper(),
                )
            )
        return SonarResult(issues=issues)


def get_sonarqube_client() -> SonarQubeClient:
    """Build a client from settings (disabled unless SONARQUBE_ENABLED=true and configured)."""
    settings = get_settings()
    return SonarQubeClient(
        enabled=settings.sonarqube_enabled,
        base_url=settings.sonarqube_url,
        token=settings.sonarqube_token,
        project_key=settings.sonarqube_project_key,
        timeout=settings.sonarqube_timeout,
    )
