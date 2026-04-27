"""Abstract base class for Jira providers."""

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
    def create_issue(
        self,
        project_key: str,
        summary: str,
        issue_type: str,
        description: str = "",
        fields: dict | None = None,
    ) -> tuple[dict, str]:
        """Create a new Jira issue. Returns (issue_dict, status)."""

    @abstractmethod
    def update_issue_fields(
        self, ticket_key: str, fields: dict
    ) -> tuple[dict | None, str]:
        """Update issue fields. Returns (result_dict | None, status)."""

    @abstractmethod
    def change_assignee(
        self, ticket_key: str, account_id: str | None
    ) -> tuple[str | None, str]:
        """Change the assignee. Returns (account_id | None, status)."""

    @abstractmethod
    def add_comment(
        self, ticket_key: str, text: str, adf_body: dict | None = None
    ) -> tuple[str | None, str]:
        """Add a comment. Returns (comment_id | None, status)."""

    @abstractmethod
    def update_comment(
        self,
        ticket_key: str,
        comment_id: str,
        new_text: str,
        adf_body: dict | None = None,
    ) -> tuple[str | None, str]:
        """Update a comment. Returns (comment_id | None, status)."""

    @abstractmethod
    def delete_comment(
        self, ticket_key: str, comment_id: str
    ) -> tuple[str | None, str]:
        """Delete a comment. Returns (comment_id | None, status)."""

    @property
    def backend_name(self) -> str:
        return "unknown"
