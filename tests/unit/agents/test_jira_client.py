"""Unit tests for the v2 Jira REST client."""

from agents.jira.client import JiraClient


def test_request_retries_after_scoped_gateway_failure(monkeypatch):
    """Cloud requests should fall back to the site-local API on 401/403/404."""
    client = JiraClient("https://example.atlassian.net", "token", "user@example.com")
    calls: list[str] = []

    monkeypatch.setattr(
        client,
        "_candidate_api_base_urls",
        lambda: [
            "https://api.atlassian.com/ex/jira/cloud-id/rest/api/3",
            "https://example.atlassian.net/rest/api/3",
        ],
    )

    def fake_request_once(api_base_url, method, path, payload=None, timeout=20):
        calls.append(api_base_url)
        if api_base_url.startswith("https://api.atlassian.com"):
            return 401, {"error": "unauthorized"}
        return 200, {"accountId": "user-1"}

    monkeypatch.setattr(client, "_request_once", fake_request_once)

    status, body = client.request("GET", "myself")

    assert status == 200
    assert body == {"accountId": "user-1"}
    assert calls == [
        "https://api.atlassian.com/ex/jira/cloud-id/rest/api/3",
        "https://example.atlassian.net/rest/api/3",
    ]


def test_search_expands_issue_id_only_payload(monkeypatch):
    """search() should hydrate issue-id-only responses into full issue docs."""
    client = JiraClient("https://example.atlassian.net", "token", "user@example.com")

    monkeypatch.setattr(
        client,
        "_post_search",
        lambda jql, max_results=10, fields=None: (200, {"isLast": True, "issues": [{"id": "2488670"}]}),
    )

    def fake_request(method, path, payload=None, timeout=20):
        assert method == "GET"
        assert path == "issue/2488670"
        return 200, {
            "id": "2488670",
            "key": "PROJ-123",
            "fields": {"summary": "Ticket from hydration"},
        }

    monkeypatch.setattr(client, "request", fake_request)

    result, status = client.search("key = PROJ-123", max_results=5)

    assert status == "ok"
    assert result["issues"][0]["key"] == "PROJ-123"
    assert result["issues"][0]["fields"]["summary"] == "Ticket from hydration"