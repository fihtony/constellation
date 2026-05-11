"""Jira REST API v3 provider — wraps JiraClient into the JiraProvider interface.

This is a thin adapter around ``agents.jira.client.JiraClient`` so it conforms
to the pluggable provider contract while reusing the client's scoped-cloud
request handling.
"""
from __future__ import annotations

from agents.jira.client import JiraClient
from agents.jira.providers.base import JiraProvider


class JiraRESTProvider(JiraProvider):
    """Jira provider backed by the Jira Cloud REST API v3."""

    def __init__(
        self,
        base_url: str,
        token: str,
        email: str = "",
        auth_mode: str = "basic",
        corp_ca_bundle: str = "",
        cloud_id: str = "",
        api_base_url: str = "",
    ) -> None:
        self._client = JiraClient(
            base_url=base_url,
            token=token,
            email=email,
            auth_mode=auth_mode,
            corp_ca_bundle=corp_ca_bundle,
            cloud_id=cloud_id,
            api_base_url=api_base_url,
        )

    # -- JiraProvider interface ---------------------------------------------

    def get_myself(self) -> tuple[dict, str]:
        return self._client.get_myself()

    def fetch_issue(self, ticket_key: str) -> tuple[dict | None, str]:
        return self._client.fetch_ticket(ticket_key)

    def search_issues(
        self, jql: str, max_results: int = 10, fields: list | None = None
    ) -> tuple[dict, str]:
        return self._client.search(jql, max_results, fields)

    def get_transitions(self, ticket_key: str) -> tuple[list, str]:
        return self._client.get_transitions(ticket_key)

    def transition_issue(
        self, ticket_key: str, transition_name: str
    ) -> tuple[str | None, str]:
        transitions, status = self._client.get_transitions(ticket_key)
        if status != "ok":
            return None, f"could_not_fetch_transitions: {status}"

        target_lower = transition_name.strip().lower()
        match = None
        for transition in transitions:
            if not isinstance(transition, dict):
                continue
            name = transition.get("name", "")
            if name.lower() == target_lower or name.lower().startswith(target_lower):
                match = transition
                break

        if not match:
            available = [t.get("name") for t in transitions if isinstance(t, dict)]
            return None, f"transition_not_found (available: {available})"

        transition_id = match.get("id")
        if not transition_id:
            return None, "transition_missing_id"

        request_status, _body = self._client.request(
            "POST",
            f"issue/{ticket_key}/transitions",
            {"transition": {"id": transition_id}},
        )
        if request_status in (200, 204):
            return transition_id, f"transitioned_to:{match.get('name', transition_name)}"
        return None, f"HTTP {request_status}"

    def add_comment(
        self, ticket_key: str, text: str, adf_body: dict | None = None
    ) -> tuple[str | None, str]:
        data, status = self._client.add_comment(ticket_key, text)
        comment_id = data.get("id", "") if isinstance(data, dict) else ""
        return comment_id or None, status

    def update_issue_fields(
        self, ticket_key: str, fields: dict
    ) -> tuple[dict | None, str]:
        if not fields:
            return None, "missing_fields"
        request_status, _body = self._client.request(
            "PUT",
            f"issue/{ticket_key}",
            {"fields": fields},
        )
        if request_status in (200, 204):
            return {"ticketKey": ticket_key}, "updated"
        return None, f"HTTP {request_status}"

    @property
    def backend_name(self) -> str:
        return "rest"
