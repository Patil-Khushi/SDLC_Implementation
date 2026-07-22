"""GitHub integration — opens/finds a dev -> main pull request for the finalize step.

Per DEVELOPER_GUIDE.md rule 6 (outside tools live in ``integrations/``), the finalize node
(``app/graph/nodes.py::finalize_node``) calls this wrapper — it never talks to the GitHub API
directly. GitHub is a network service, not a sandbox command, so this uses ``httpx`` from the
service host (like ``integrations/sonarqube.py``) rather than the exec-sandbox Executor.

Design notes:
- ``create_or_update_pull_request`` is idempotent: it looks for an existing OPEN PR for the same
  head/base pair first and returns that instead of creating a duplicate — safe to call again on a
  retry without piling up PRs.
- It only ever OPENS a pull request, never merges one. The `dev -> main` merge is left for a human
  to approve on GitHub — a deliberate safety choice for a shared remote, not a missing feature.
- ``http_request`` is injectable so tests exercise this without a live GitHub token or network.
- Never raises — a GitHub API failure degrades to ``PRResult(ok=False, error=...)`` so a flaky
  API/network blip doesn't crash a run that otherwise passed Security.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.config.settings import get_settings

#: A minimal seam over the HTTP layer: (method, url, params, json_body, headers, timeout) ->
#: (status_code, parsed JSON body). Lets tests exercise this integration without the network.
HttpRequest = Callable[[str, str, dict[str, Any], dict[str, Any] | None, dict[str, str], float], tuple[int, Any]]


@dataclass(frozen=True)
class PRResult:
    """Outcome of create_or_update_pull_request (never raises)."""

    ok: bool
    number: int | None = None
    url: str = ""
    error: str = ""


class GitHubClient(ABC):
    """Opens (or finds an existing) pull request between two branches of one repo."""

    @abstractmethod
    def create_or_update_pull_request(
        self, owner: str, repo: str, head: str, base: str, title: str, body: str
    ) -> PRResult:
        """Return the existing open PR for head->base if one exists, else create it."""


# --------------------------------------------------------------------------- fake impl


class FakeGitHubClient(GitHubClient):
    """In-memory, scriptable client for unit tests — no network.

    ``existing`` seeds a pre-existing PR to return instead of creating one, keyed by
    ``"owner/repo/head/base"``. Every call is recorded in :attr:`calls` so tests can assert
    idempotency (one PR per head/base pair, not one per loop iteration/retry).
    """

    def __init__(self, *, existing: dict[str, PRResult] | None = None) -> None:
        self._existing = dict(existing or {})
        self._created: dict[str, PRResult] = {}
        self.calls: list[dict[str, str]] = []
        self._next_number = 1000

    def create_or_update_pull_request(
        self, owner: str, repo: str, head: str, base: str, title: str, body: str
    ) -> PRResult:
        self.calls.append({"owner": owner, "repo": repo, "head": head, "base": base, "title": title})
        key = f"{owner}/{repo}/{head}/{base}"
        if key in self._existing:
            return self._existing[key]
        if key not in self._created:
            self._created[key] = PRResult(
                ok=True, number=self._next_number,
                url=f"https://github.com/{owner}/{repo}/pull/{self._next_number}",
            )
            self._next_number += 1
        return self._created[key]


# --------------------------------------------------------------------------- real impl


class RealGitHubClient(GitHubClient):
    """Real client backed by the GitHub REST API (PAT-authenticated via ``Authorization: Bearer``)."""

    _API = "https://api.github.com"

    def __init__(self, *, token: str, timeout: float = 30.0, http_request: HttpRequest | None = None) -> None:
        self._token = token
        self._timeout = timeout
        self._http_request = http_request  # injected in tests; real httpx built lazily otherwise

    def create_or_update_pull_request(
        self, owner: str, repo: str, head: str, base: str, title: str, body: str
    ) -> PRResult:
        if not self._token:
            return PRResult(ok=False, error="github_pat not configured")
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json"}
        try:
            existing = self._find_open_pr(owner, repo, head, base, headers)
            if existing is not None:
                return existing
            status, payload = self._request(
                "POST", f"{self._API}/repos/{owner}/{repo}/pulls",
                json_body={"title": title, "head": head, "base": base, "body": body},
                headers=headers,
            )
        except Exception as exc:  # noqa: BLE001 - a GitHub API failure must not crash the run
            return PRResult(ok=False, error=f"github request failed: {exc}")
        if status not in (200, 201):
            message = payload.get("message", "") if isinstance(payload, dict) else ""
            return PRResult(ok=False, error=f"github create PR failed ({status}): {message}".strip())
        return PRResult(ok=True, number=payload.get("number"), url=payload.get("html_url", ""))

    def _find_open_pr(self, owner: str, repo: str, head: str, base: str,
                       headers: dict[str, str]) -> PRResult | None:
        status, payload = self._request(
            "GET", f"{self._API}/repos/{owner}/{repo}/pulls",
            params={"head": f"{owner}:{head}", "base": base, "state": "open"}, headers=headers,
        )
        if status != 200 or not isinstance(payload, list) or not payload:
            return None
        pr = payload[0]
        return PRResult(ok=True, number=pr.get("number"), url=pr.get("html_url", ""))

    def _request(self, method: str, url: str, *, params: dict[str, Any] | None = None,
                 json_body: dict[str, Any] | None = None, headers: dict[str, str]) -> tuple[int, Any]:
        if self._http_request is not None:
            return self._http_request(method, url, params or {}, json_body, headers, self._timeout)
        import httpx  # lazy: keep module import free of a hard httpx dependency at import time

        response = httpx.request(method, url, params=params, json=json_body, headers=headers, timeout=self._timeout)
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body


# --------------------------------------------------------------------------- provider


def get_github_client() -> GitHubClient:
    """Build a client from settings (real, PAT-authenticated)."""
    settings = get_settings()
    return RealGitHubClient(token=settings.github_pat)
