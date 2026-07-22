"""GitHub integration tests - FakeGitHubClient's create-or-find-existing PR behavior, and
RealGitHubClient's request-building logic against an injected HTTP seam (no live network/token).
"""

from __future__ import annotations

from app.integrations.github import FakeGitHubClient, PRResult, RealGitHubClient


def test_creates_a_new_pr() -> None:
    client = FakeGitHubClient()
    result = client.create_or_update_pull_request("acme", "app", "dev", "main", "title", "body")
    assert result.ok
    assert result.number == 1000
    assert result.url == "https://github.com/acme/app/pull/1000"
    assert client.calls == [{"owner": "acme", "repo": "app", "head": "dev", "base": "main", "title": "title"}]


def test_repeated_calls_for_the_same_head_base_are_idempotent() -> None:
    client = FakeGitHubClient()
    first = client.create_or_update_pull_request("acme", "app", "dev", "main", "t1", "b1")
    second = client.create_or_update_pull_request("acme", "app", "dev", "main", "t2", "b2")
    assert first == second
    assert len(client.calls) == 2  # both calls recorded, but only one PR was ever "created"


def test_seeded_existing_pr_is_returned_without_creating_a_new_one() -> None:
    existing = PRResult(ok=True, number=42, url="https://github.com/acme/app/pull/42")
    client = FakeGitHubClient(existing={"acme/app/dev/main": existing})
    result = client.create_or_update_pull_request("acme", "app", "dev", "main", "title", "body")
    assert result == existing


def test_different_head_base_pairs_get_distinct_prs() -> None:
    client = FakeGitHubClient()
    a = client.create_or_update_pull_request("acme", "app", "dev", "main", "t", "b")
    b = client.create_or_update_pull_request("acme", "app", "feature-x", "main", "t", "b")
    assert a.number != b.number


def test_real_client_creates_when_no_open_pr_exists() -> None:
    calls: list[tuple[str, str]] = []

    def http_request(method, url, params, json_body, headers, timeout):
        calls.append((method, url))
        if method == "GET":
            return 200, []  # no existing open PR
        assert json_body == {"title": "t", "head": "dev", "base": "main", "body": "b"}
        return 201, {"number": 7, "html_url": "https://github.com/acme/app/pull/7"}

    client = RealGitHubClient(token="tok", http_request=http_request)
    result = client.create_or_update_pull_request("acme", "app", "dev", "main", "t", "b")
    assert result == PRResult(ok=True, number=7, url="https://github.com/acme/app/pull/7")
    assert [m for m, _ in calls] == ["GET", "POST"]


def test_real_client_returns_existing_open_pr_without_posting() -> None:
    def http_request(method, url, params, json_body, headers, timeout):
        assert method == "GET", "must not POST when an open PR already exists"
        return 200, [{"number": 3, "html_url": "https://github.com/acme/app/pull/3"}]

    client = RealGitHubClient(token="tok", http_request=http_request)
    result = client.create_or_update_pull_request("acme", "app", "dev", "main", "t", "b")
    assert result == PRResult(ok=True, number=3, url="https://github.com/acme/app/pull/3")


def test_real_client_reports_api_error_without_raising() -> None:
    def http_request(method, url, params, json_body, headers, timeout):
        if method == "GET":
            return 200, []
        return 422, {"message": "Validation Failed"}

    client = RealGitHubClient(token="tok", http_request=http_request)
    result = client.create_or_update_pull_request("acme", "app", "dev", "main", "t", "b")
    assert not result.ok
    assert "422" in result.error and "Validation Failed" in result.error


def test_real_client_without_token_is_a_clean_noop() -> None:
    def _boom(*a, **k):
        raise AssertionError("must not make an HTTP call without a token")

    client = RealGitHubClient(token="", http_request=_boom)
    result = client.create_or_update_pull_request("acme", "app", "dev", "main", "t", "b")
    assert not result.ok
    assert "not configured" in result.error
