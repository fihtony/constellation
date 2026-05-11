"""Jira REST API v3 provider — wraps JiraClient into the JiraProvider interface.

This is a thin adapter around the existing ``agents.jira.client.JiraClient``
so it conforms to the pluggable provider contract.
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
    ) -> None:
        self._client = JiraClient(
            base_url=base_url,
            token=token,
            email=email,
            auth_mode=auth_mode,
            corp_ca_bundle=corp_ca_bundle,
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
        # REST provider resolves transition name → id and POSTs
        transitions, status = self._client.get_transitions(ticket_key)
        if status != "ok":
            return None, f"could_not_fetch_transitions: {status}"
        target_lower = transition_name.strip().lower()
        match = None
        for t in transitions:
            if not isinstance(t, dict):
                continue
            name = t.get("name", "")
            if name.lower() == target_lower or name.lower().startswith(target_lower):
                match = t
                break
        if not match:
            available = [t.get("name") for t in transitions if isinstance(t, dict)]
            return None, f"transition_not_found (available: {available})"
        tid = match.get("id")
        if not tid:
            return None, "transition_missing_id"
        # POST transition
        from urllib.error import HTTPError
        try:
            self._client._post(f"/issue/{ticket_key}/transitions", {"transition": {"id": tid}})
            return tid, f"transitioned_to:{match.get('name', transition_name)}"
        except HTTPError as exc:
            return None, f"HTTP {exc.code}"
        except Exception as exc:
            return None, str(exc)

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
        from urllib.error import HTTPError
        try:
            self._client._request("PUT", f"/issue/{ticket_key}", {"fields": fields})
            return {"ticketKey": ticket_key}, "updated"
        except HTTPError as exc:
            return None, f"HTTP {exc.code}"
        except Exception as exc:
            return None, str(exc)

    @property
    def backend_name(self) -> str:
        return "rest"
