"""Abstract base class for Jira providers.

All Jira backends (REST, MCP) implement this interface so the adapter
layer can switch backends transparently.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class JiraProvider(ABC):
    """Interface all Jira providers must implement."""

    @abstractmethod
    def get_myself(self) -> tuple[dict, str]:
        """Return (user_dict, status) for the authenticated user."""

    @abstractmethod
    def fetch_issue(self, ticket_key: str) -> tuple[dict | None, str]:
        """Fetch a Jira issue. Returns (issue_dict | None, status)."""

    @abstractmethod
    def search_issues(
        self, jql: str, max_results: int = 10, fields: list | None = None
    ) -> tuple[dict, str]:
        """Search issues via JQL. Returns (search_result_dict, status)."""

    @abstractmethod
    def get_transitions(self, ticket_key: str) -> tuple[list, str]:
        """List available transitions for a ticket. Returns (transitions_list, status)."""

    @abstractmethod
    def transition_issue(
        self, ticket_key: str, transition_name: str
    ) -> tuple[str | None, str]:
        """Transition a ticket by transition name. Returns (transition_id | None, status)."""

    @abstractmethod
    def add_comment(
        self, ticket_key: str, text: str, adf_body: dict | None = None
    ) -> tuple[str | None, str]:
        """Add a comment. Returns (comment_id | None, status)."""

    @abstractmethod
    def update_issue_fields(
        self, ticket_key: str, fields: dict
    ) -> tuple[dict | None, str]:
        """Update issue fields. Returns (result_dict | None, status)."""

    @abstractmethod
    def list_comments(
        self, ticket_key: str, max_results: int = 50
    ) -> tuple[list, str]:
        """List comments on a ticket. Returns (comments_list, status)."""

    @property
    def backend_name(self) -> str:
        """Return the backend identifier."""
        return "unknown"
