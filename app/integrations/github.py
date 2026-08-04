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

Credential resolution (why more than one token):
    Opening a PR needs WRITE access to the product repo — but the product repo is created and
    pushed by the Executor via the ``gh`` CLI (``gh repo create`` / ``gh auth setup-git``), which
    authenticates as whoever is logged into ``gh``. That is frequently a DIFFERENT identity from
    ``$GITHUB_PAT``. When it is, the PAT has read-only access to a repo it does not own and
    ``POST /pulls`` fails 403 — the run finished green but silently produced no PR (observed live:
    PAT identity ``Patil-Khushi`` = ``pull`` only vs. repo owner ``Gaurav-Patil-1695`` = ``push``).
    So every distinct available credential is tried in turn — run token, configured PAT, then the
    ``gh`` CLI's own token — and only permission-shaped failures (401/403/404) advance to the next
    one. A real validation error (422 "No commits between …") is reported as-is, never retried.
"""

from __future__ import annotations

import logging
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

#: HTTP statuses that mean "this credential cannot do this", not "this request was wrong" — the
#: only ones worth re-attempting with a different token. 401 bad/expired, 403 no write access,
#: 404 repo not visible to this identity (GitHub hides private/foreign repos behind 404).
_CREDENTIAL_STATUSES = (401, 403, 404)

#: A minimal seam over the HTTP layer: (method, url, params, json_body, headers, timeout) ->
#: (status_code, parsed JSON body). Lets tests exercise this integration without the network.
HttpRequest = Callable[[str, str, dict[str, Any], dict[str, Any] | None, dict[str, str], float], tuple[int, Any]]


@dataclass(frozen=True)
class PRResult:
    """Outcome of create_or_update_pull_request (never raises).

    ``status`` carries the GitHub HTTP status when there was one, so a caller (and the retry logic
    here) can tell a credential problem apart from a genuine validation error.
    """

    ok: bool
    number: int | None = None
    url: str = ""
    error: str = ""
    status: int | None = None


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

    def __init__(self, *, token: str, fallback_tokens: tuple[str, ...] = (),
                 timeout: float = 30.0, http_request: HttpRequest | None = None) -> None:
        self._token = token
        self._fallback_tokens = fallback_tokens
        self._timeout = timeout
        self._http_request = http_request  # injected in tests; real httpx built lazily otherwise

    def _credentials(self) -> list[str]:
        """Every distinct non-empty credential to try, in preference order (see module docstring)."""
        ordered: list[str] = []
        for token in (self._token, *self._fallback_tokens):
            if token and token not in ordered:
                ordered.append(token)
        return ordered

    def create_or_update_pull_request(
        self, owner: str, repo: str, head: str, base: str, title: str, body: str
    ) -> PRResult:
        credentials = self._credentials()
        if not credentials:
            return PRResult(
                ok=False,
                error="no GitHub credential available (github_pat unset and `gh auth token` empty)",
            )
        result = PRResult(ok=False, error="no attempt made")
        for i, token in enumerate(credentials):
            result = self._attempt(owner, repo, head, base, title, body, token)
            if result.ok or result.status not in _CREDENTIAL_STATUSES:
                return result  # success, or a real error that another token would not fix
            if i + 1 < len(credentials):
                # The identity that pushed the repo is often not the identity in $GITHUB_PAT; say
                # so explicitly, because a silent 403 here is exactly how a run ends with no PR.
                logger.warning(
                    "[github] credential %d/%d cannot open the PR (%s) — retrying with the next "
                    "available credential", i + 1, len(credentials), result.error,
                )
        return result

    def _attempt(self, owner: str, repo: str, head: str, base: str, title: str, body: str,
                 token: str) -> PRResult:
        """One full find-then-create pass with a single credential."""
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
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
            return PRResult(
                ok=False, status=status,
                error=f"github create PR failed ({status}): {message}".strip(),
            )
        return PRResult(ok=True, number=payload.get("number"), url=payload.get("html_url", ""),
                        status=status)

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


def gh_cli_token() -> str:
    """The ``gh`` CLI's own token — the SAME credential the Executor uses to create and push the
    product repo (``gh repo create`` / ``gh auth setup-git``), hence the one guaranteed to have
    write access to it. Empty string when ``gh`` is absent or not logged in."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("[github] `gh auth token` unavailable: %s", exc)
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def get_github_client(*, token: str | None = None) -> GitHubClient:
    """Build a real client that tries every credential available to this run, in order:

    1. ``token`` — the credential THIS run pushed with (``state["git_token"]``), when set;
    2. ``settings.github_pat`` — the configured PAT;
    3. the ``gh`` CLI's token — whoever owns the repo the Executor just created.

    Opening the PR needs write access, and (1)/(2) belong to whoever configured the service while
    (3) belongs to whoever ``gh`` created the repo as. Those differ often enough that trying only
    one is why a green run could end with no PR at all — see the module docstring.
    """
    settings = get_settings()
    return RealGitHubClient(
        token=(token or "").strip() or settings.github_pat,
        fallback_tokens=(settings.github_pat, gh_cli_token()),
    )
