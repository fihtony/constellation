"""Connector base class and NormalizedMessage for the unified IM Gateway."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class NormalizedMessage:
    """Platform-agnostic inbound message representation."""

    channel: str  # "teams" | "slack" | "lark" | ...
    user_id: str  # Platform-specific unique user identifier
    workspace_id: str  # Teams tenant_id / Slack team_id / ...
    text: str  # Normalized plain text
    command: str  # Parsed command (e.g. "/tasks") or ""
    command_args: str  # Args after the command or ""
    session_mode: str = "personal"  # personal | shared-session | team-scoped
    reply_target: dict = field(default_factory=dict)  # Platform-specific delivery info
    thread_ref: str = ""  # Teams conversation_id / Slack thread_ts / ...
    raw_payload: dict = field(default_factory=dict)  # Original platform payload (for audit)
    is_duplicate: bool = False


class IMConnector(ABC):
    """Abstract connector interface that each IM platform must implement.

    Connectors handle:
    - Request validation (signatures, tokens)
    - Inbound message parsing -> NormalizedMessage
    - Outbound rendering (task cards, notifications)
    - Proactive message delivery
    """

    @property
    @abstractmethod
    def channel_id(self) -> str:
        """Platform identifier: 'teams', 'slack', 'lark', etc."""
        ...

    @property
    def requires_immediate_ack(self) -> bool:
        """Whether inbound HTTP requests must be ACKed within 3 seconds.

        Slack = True (Events API requires fast ACK).
        Teams = False (Bot Framework waits for response body).
        """
        return False

    @classmethod
    @abstractmethod
    def is_configured(cls, config: dict) -> bool:
        """Return True if all required env vars / config for this connector are present."""
        ...

    @abstractmethod
    def validate_request(self, headers: dict, body: bytes) -> bool:
        """Validate inbound request authenticity (signature, token, etc.)."""
        ...

    @abstractmethod
    def normalize_inbound(self, raw_payload: dict, headers: dict | None = None) -> NormalizedMessage | None:
        """Parse platform-specific payload into a NormalizedMessage.

        Return None if the payload should be silently ignored (e.g. bot echo, duplicate).
        """
        ...

    # -- Outbound rendering (return platform-native payloads) --

    @abstractmethod
    def render_task_created(self, task_id: str, summary: str) -> dict:
        ...

    @abstractmethod
    def render_task_list(self, tasks: list[dict]) -> dict:
        ...

    @abstractmethod
    def render_task_detail(self, task: dict) -> dict:
        ...

    @abstractmethod
    def render_input_required(self, question: str, task_id: str) -> dict:
        ...

    @abstractmethod
    def render_task_completed(self, task_id: str, summary: str, links: list[dict] | None = None) -> dict:
        ...

    @abstractmethod
    def render_task_failed(self, task_id: str, error_summary: str) -> dict:
        ...

    @abstractmethod
    def render_help(self) -> dict:
        ...

    @abstractmethod
    def render_error(self, message: str) -> dict:
        ...

    @abstractmethod
    def send_message(self, target: dict, content: dict) -> str:
        """Send a proactive message to the given delivery target.

        Returns: 'ok' | 'unauthorized' | 'rate_limited' | 'error'
        """
        ...
