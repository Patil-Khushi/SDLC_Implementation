"""Why a green run could finish with NO pull request — and the credential fallback that fixes it.

Observed live (run ``resources-app-0723-232102``): the Executor creates and pushes the product
repo through the ``gh`` CLI, so the repo is owned by the ``gh`` identity — while ``finalize_node``
opened the PR with ``$GITHUB_PAT``, a DIFFERENT identity holding only ``pull`` on that repo. GitHub
answers ``POST /pulls`` with 403, ``finalize_status`` became ``pr_failed``, and the only trace was
one log line: the run still reported "completed" and produced its zip.

Covered here: permission-shaped failures advance to the next credential, real validation errors do
not, and every finalize outcome is written into ``generation_summary`` so it can never be silent.
"""

from __future__ import annotations

from typing import Any

from app.graph import nodes as nodes_module
from app.graph.nodes import finalize_node
from app.graph.state import new_state
from app.integrations.github import PRResult, RealGitHubClient

_PR_URL = "https://github.com/owner/repo/pull/7"
_REPO_URL = "https://github.com/owner/repo"

# Which token GitHub will accept as having write access, in these fakes.
_WRITE_TOKEN = "gh-cli-token-owns-the-repo"
_READONLY_TOKEN = "configured-pat-different-identity"


#: What GitHub ACTUALLY returns when a non-collaborator PAT tries to open a PR — captured live.
#: A 422, not a 403, with the real reason buried in errors[]. See _PERMISSION_HINTS.
_NOT_COLLABORATOR = (422, {
    "message": "Validation Failed",
    "errors": [{"resource": "Issue", "code": "custom", "message": "must be a collaborator"}],
})


def _fake_api(write_token: str, *, create_status: int = 201, create_message: str = "",
              denial: tuple[int, dict[str, Any]] = _NOT_COLLABORATOR,
              ) -> tuple[Any, list[dict[str, str]]]:
    """A GitHub stand-in that only honors ``write_token``; every call is recorded for assertions.

    ``denial`` is how the API rebuffs any other token — defaults to the live-captured 422.
    """
    calls: list[dict[str, str]] = []

    def http_request(method: str, url: str, params: dict[str, Any], json_body: dict[str, Any] | None,
                     headers: dict[str, str], timeout: float) -> tuple[int, Any]:
        token = headers.get("Authorization", "").removeprefix("Bearer ")
        calls.append({"method": method, "token": token})
        if method == "GET":
            # A read-only PAT CAN list PRs on a public repo (verified live: 200) — the denial only
            # bites on create, which is exactly why the failure was so easy to misread.
            return 200, []
        if token != write_token:
            return denial
        if create_status not in (200, 201):
            return create_status, {"message": create_message}
        return create_status, {"number": 7, "html_url": _PR_URL}

    return http_request, calls


def test_non_collaborator_422_falls_back_instead_of_giving_up() -> None:
    # THE live case, and the one a status-only rule gets wrong: GitHub denies a non-collaborator
    # PAT with 422 "Validation Failed" + "must be a collaborator" — not 403. Treating 422 as
    # always-terminal means never trying the token that owns the repo, i.e. no PR at all.
    http_request, calls = _fake_api(_WRITE_TOKEN, denial=_NOT_COLLABORATOR)
    client = RealGitHubClient(
        token=_READONLY_TOKEN, fallback_tokens=(_WRITE_TOKEN,), http_request=http_request,
    )

    result = client.create_or_update_pull_request("owner", "repo", "dev", "main", "t", "b")

    assert result.ok, f"422/collaborator was not recognized as a credential problem: {result.error}"
    assert result.url == _PR_URL
    assert [c["token"] for c in calls if c["method"] == "POST"] == [_READONLY_TOKEN, _WRITE_TOKEN]


def test_error_message_includes_the_errors_array_detail() -> None:
    # "Validation Failed" alone is true but useless — the actionable reason is in errors[].
    http_request, _ = _fake_api("some-other-token", denial=_NOT_COLLABORATOR)
    client = RealGitHubClient(token=_READONLY_TOKEN, http_request=http_request)

    result = client.create_or_update_pull_request("owner", "repo", "dev", "main", "t", "b")

    assert not result.ok
    assert "Validation Failed" in result.error
    assert "must be a collaborator" in result.error  # the part that tells a human what to do


def test_readonly_pat_falls_back_when_denial_is_a_plain_403() -> None:
    # The other shape of the same problem (403 rather than 422) must also fall back.
    http_request, calls = _fake_api(
        _WRITE_TOKEN,
        denial=(403, {"message": "Resource not accessible by personal access token"}),
    )
    client = RealGitHubClient(
        token=_READONLY_TOKEN, fallback_tokens=(_WRITE_TOKEN,), http_request=http_request,
    )

    result = client.create_or_update_pull_request("owner", "repo", "dev", "main", "t", "b")

    assert result.ok, f"fallback did not recover: {result.error}"
    assert result.url == _PR_URL
    posts = [c["token"] for c in calls if c["method"] == "POST"]
    assert posts == [_READONLY_TOKEN, _WRITE_TOKEN]  # tried the PAT first, then the working token


def test_validation_error_is_not_retried_with_other_credentials() -> None:
    # 422 "No commits between main and dev" is a REAL error — a different token cannot fix it, so
    # burning the other credentials (and reporting the last one's error) would only mislead.
    http_request, calls = _fake_api(
        _WRITE_TOKEN, create_status=422, create_message="No commits between main and dev",
    )
    client = RealGitHubClient(
        token=_WRITE_TOKEN, fallback_tokens=(_READONLY_TOKEN,), http_request=http_request,
    )

    result = client.create_or_update_pull_request("owner", "repo", "dev", "main", "t", "b")

    assert not result.ok
    assert result.status == 422
    assert "No commits between" in result.error
    assert [c["token"] for c in calls if c["method"] == "POST"] == [_WRITE_TOKEN]  # exactly one try


def test_duplicate_and_empty_credentials_are_collapsed() -> None:
    # get_github_client passes (run token, settings PAT, gh token) — commonly the same value twice,
    # or empty. The same token must not be retried, and empties must not count as attempts.
    http_request, calls = _fake_api(_WRITE_TOKEN)
    client = RealGitHubClient(
        token=_READONLY_TOKEN, fallback_tokens=("", _READONLY_TOKEN, _WRITE_TOKEN),
        http_request=http_request,
    )

    assert client.create_or_update_pull_request("owner", "repo", "dev", "main", "t", "b").ok
    assert [c["token"] for c in calls if c["method"] == "POST"] == [_READONLY_TOKEN, _WRITE_TOKEN]


def test_no_credential_at_all_reports_why() -> None:
    client = RealGitHubClient(token="", fallback_tokens=("", ""))
    result = client.create_or_update_pull_request("owner", "repo", "dev", "main", "t", "b")
    assert not result.ok
    assert "no GitHub credential available" in result.error


def test_existing_open_pr_is_returned_without_creating_a_duplicate() -> None:
    def http_request(method: str, url: str, params: dict[str, Any], json_body: dict[str, Any] | None,
                     headers: dict[str, str], timeout: float) -> tuple[int, Any]:
        assert method == "GET", "must not POST when an open PR already exists"
        return 200, [{"number": 7, "html_url": _PR_URL}]

    client = RealGitHubClient(token=_WRITE_TOKEN, http_request=http_request)
    result = client.create_or_update_pull_request("owner", "repo", "dev", "main", "t", "b")
    assert result.ok and result.number == 7


# --------------------------------------------------------------------- finalize_node visibility


class _StubClient:
    """Records the token finalize handed over and returns a scripted result."""

    def __init__(self, result: PRResult) -> None:
        self._result = result
        self.token_seen: str | None = "<not called>"

    def create_or_update_pull_request(self, *_args: Any, **_kw: Any) -> PRResult:
        return self._result


def _finalize_with(monkeypatch: Any, result: PRResult, **state_kw: Any) -> dict[str, Any]:
    stub = _StubClient(result)
    captured: dict[str, Any] = {}

    def fake_get_client(*, token: str | None = None) -> _StubClient:
        captured["token"] = token
        return stub

    monkeypatch.setattr(nodes_module, "get_github_client", fake_get_client)
    state = new_state(run_id="r1", attempt=0, project_id="p1", **state_kw)
    state["repo_url"] = _REPO_URL
    state["branch"] = "dev"
    out = finalize_node(state)
    return {"state": out, "token": captured.get("token")}


def test_finalize_passes_the_runs_own_push_credential(monkeypatch: Any) -> None:
    # The token this run pushed with is the one most likely to have write access — it must be
    # offered first rather than being ignored in favor of the ambient PAT.
    got = _finalize_with(monkeypatch, PRResult(ok=True, url=_PR_URL), git_token="run-push-token")
    assert got["token"] == "run-push-token"
    assert got["state"]["finalize_status"] == "pr_created"
    assert got["state"]["pr_url"] == _PR_URL


def test_finalize_records_the_pr_url_in_the_run_summary(monkeypatch: Any) -> None:
    got = _finalize_with(monkeypatch, PRResult(ok=True, url=_PR_URL))
    assert _PR_URL in got["state"]["generation_summary"]


def test_finalize_failure_is_written_to_the_run_summary_not_only_the_log(monkeypatch: Any) -> None:
    # The heart of the live miss: a 403 left `pr_failed` on state and one log line, while the run
    # went on to report "completed" with a zip. The reason must land in the summary a human reads.
    got = _finalize_with(
        monkeypatch,
        PRResult(ok=False, status=403, error="github create PR failed (403): not accessible"),
    )
    summary = got["state"]["generation_summary"]
    assert got["state"]["finalize_status"] == "pr_failed"
    assert "PR FAILED" in summary
    assert "403" in summary
    assert "dev -> main on owner/repo" in summary  # which PR, on which repo


def test_finalize_without_repo_url_says_why_it_skipped(monkeypatch: Any) -> None:
    stub = _StubClient(PRResult(ok=True, url=_PR_URL))
    monkeypatch.setattr(nodes_module, "get_github_client", lambda **_kw: stub)
    state = new_state(run_id="r1", attempt=0, project_id="p1")  # no repo_url (a --no-publish run)

    out = finalize_node(state)

    assert out["finalize_status"] == "skipped"
    assert "SKIPPED" in out["generation_summary"] and "no repo_url" in out["generation_summary"]
    assert "pr_url" not in out or not out.get("pr_url")
