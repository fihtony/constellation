"""Slack Connector — Slack Events API / Socket Mode adapter for im-gateway.

Implements the Slack Connector design from docs/compass-slack-integration-zh.md §3.

Key Slack constraints handled here:
- All inbound requests must be ACKed within 3 seconds (requires_immediate_ack = True).
- Signature validation via HMAC-SHA256 (X-Slack-Signature / X-Slack-Request-Timestamp).
- Inbound text normalization: <@U123> mentions, <#C123|name> channels, <url|label> links.
- Outbound rendering uses Block Kit + mrkdwn (no Adaptive Cards).
- Proactive messaging via chat.postMessage with optional thread_ts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from im_gateway.connectors import IMConnector, NormalizedMessage
from im_gateway.connectors.registry import register_connector
from im_gateway.connectors.slack import blocks
from im_gateway.connectors.slack.normalizer import normalize_text

MAX_BLOCK_TEXT_LEN = 3000
MAX_BLOCKS = 50


class SlackConnector(IMConnector):
    """Slack Events API / Socket Mode connector."""

    @property
    def channel_id(self) -> str:
        return "slack"

    @property
    def requires_immediate_ack(self) -> bool:
        return True

    def __init__(self, config: dict):
        self._bot_token = config.get("SLACK_BOT_TOKEN", "")
        self._signing_secret = config.get("SLACK_SIGNING_SECRET", "")
        self._app_token = config.get("SLACK_APP_TOKEN", "")  # for Socket Mode

    @classmethod
    def is_configured(cls, config: dict) -> bool:
        return bool(config.get("SLACK_BOT_TOKEN"))

    # ---- Validation ----

    def validate_request(self, headers: dict, body: bytes) -> bool:
        if not self._signing_secret:
            return True  # Dev mode — no signing secret configured

        timestamp = headers.get("X-Slack-Request-Timestamp", "")
        signature = headers.get("X-Slack-Signature", "")
        if not timestamp or not signature:
            return False

        # Reject requests older than 5 minutes (replay protection)
        try:
            if abs(time.time() - float(timestamp)) > 300:
                return False
        except (ValueError, TypeError):
            return False

        sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
        computed = "v0=" + hmac.new(
            self._signing_secret.encode("utf-8"),
            sig_basestring.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(computed, signature)

    # ---- Inbound ----

    def normalize_inbound(self, raw_payload: dict, headers: dict | None = None) -> NormalizedMessage | None:
        # Handle URL verification challenge
        if raw_payload.get("type") == "url_verification":
            return None

        # Events API wrapper
        event = raw_payload.get("event", {})
        event_type = event.get("type", "")

        # Only handle message events (DM)
        if event_type != "message":
            return None

        # Ignore bot messages and message_changed subtypes
        if event.get("bot_id") or event.get("subtype"):
            return None

        user_id = event.get("user", "")
        team_id = raw_payload.get("team_id", "") or event.get("team", "")
        channel = event.get("channel", "")
        text = event.get("text", "")
        thread_ts = event.get("thread_ts", "") or event.get("ts", "")

        # Normalize Slack special tokens
        text = normalize_text(text)
        if not text:
            return None

        # Parse commands (/compass tasks -> command="/compass", args="tasks")
        command, command_args = "", text
        stripped = text.strip()
        if stripped.startswith("/"):
            parts = stripped.split(None, 1)
            command = parts[0].lower()
            command_args = parts[1] if len(parts) > 1 else ""

        return NormalizedMessage(
            channel="slack",
            user_id=user_id,
            workspace_id=team_id,
            text=text,
            command=command,
            command_args=command_args,
            reply_target={
                "channel": channel,
                "thread_ts": thread_ts,
            },
            thread_ref=thread_ts,
            raw_payload=raw_payload,
        )

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Normalize Slack-specific tokens to plain text.

        Delegates to the standalone normalizer module. This static method
        is kept for backward compatibility with tests that call it directly.
        """
        return normalize_text(text)

    # ---- Outbound rendering (Block Kit + mrkdwn) ----

    def render_task_created(self, task_id: str, summary: str) -> dict:
        return blocks.task_created(task_id, summary)

    def render_task_list(self, tasks: list[dict]) -> dict:
        return blocks.task_list(tasks)

    def render_task_detail(self, task: dict) -> dict:
        return blocks.task_detail(task)

    def render_input_required(self, question: str, task_id: str) -> dict:
        return blocks.input_required(question, task_id)

    def render_task_completed(self, task_id: str, summary: str, links: list[dict] | None = None) -> dict:
        return blocks.task_completed(task_id, summary, links)

    def render_task_failed(self, task_id: str, error_summary: str) -> dict:
        return blocks.task_failed(task_id, error_summary)

    def render_help(self) -> dict:
        return blocks.help_message()

    def render_error(self, message: str) -> dict:
        return blocks.error_message(message)

    # ---- Proactive messaging ----

    def send_message(self, target: dict, content: dict) -> str:
        channel = target.get("channel", "")
        thread_ts = target.get("thread_ts")
        if not channel:
            return "error"
        if not self._bot_token:
            print("[im-gateway] Slack proactive message skipped (no bot token)")
            return "ok"

        payload: dict = {
            "channel": channel,
            **content,  # includes "blocks"
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(
            "https://slack.com/api/chat.postMessage",
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self._bot_token}",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                return "ok"
            error = data.get("error", "unknown")
            if error in ("channel_not_found", "not_in_channel", "account_inactive"):
                return "unauthorized"
            if error == "ratelimited":
                return "rate_limited"
            print(f"[im-gateway] Slack API error: {error}")
            return "error"
        except HTTPError as err:
            if err.code == 429:
                return "rate_limited"
            print(f"[im-gateway] Slack HTTP error {err.code}: {err}")
            return "error"
        except Exception as err:
            print(f"[im-gateway] Slack proactive message failed: {err}")
            return "error"


# ---- Helpers (re-exported for backward compatibility with tests) ----

_state_emoji = blocks.state_emoji
_truncate = blocks.truncate


# Self-register at import time
register_connector("slack", SlackConnector)
