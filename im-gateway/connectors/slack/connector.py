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

# Slack special-token patterns
_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")
_CHANNEL_RE = re.compile(r"<#([A-Z0-9]+)\|([^>]*)>")
_LINK_RE = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")
_BARE_LINK_RE = re.compile(r"<(https?://[^>]+)>")

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
        text = self._normalize_text(text)
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
        """Normalize Slack-specific tokens to plain text."""
        if not text:
            return ""
        # <@U123> -> @user
        text = _MENTION_RE.sub(r"@\1", text)
        # <#C123|general> -> #general
        text = _CHANNEL_RE.sub(r"#\2", text)
        # <https://example.com|label> -> label (https://example.com)
        text = _LINK_RE.sub(r"\2 (\1)", text)
        # <https://example.com> -> https://example.com
        text = _BARE_LINK_RE.sub(r"\1", text)
        return text.strip()

    # ---- Outbound rendering (Block Kit + mrkdwn) ----

    def render_task_created(self, task_id: str, summary: str) -> dict:
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": "\u2705 Task Created"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Task ID:*\n{task_id}"},
                {"type": "mrkdwn", "text": "*Status:*\nWORKING"},
            ]},
        ]
        if summary:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _truncate(summary, 200)}})
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"Use `/compass task {task_id}` to check status."}
        ]})
        return {"blocks": blocks}

    def render_task_list(self, tasks: list[dict]) -> dict:
        if not tasks:
            return {"blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "No running tasks. Send a message to create one."}},
            ]}

        blocks: list[dict] = [
            {"type": "header", "text": {"type": "plain_text", "text": "Your Tasks"}},
        ]
        for t in tasks[:10]:
            tid = t.get("id") or t.get("task_id", "")
            state = t.get("state") or t.get("status", {}).get("state", "")
            summary = t.get("summary", "")[:50]
            emoji = _state_emoji(state)
            text = f"{emoji} *{tid}* — {state}"
            if summary:
                text += f"\n{summary}"
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        if len(tasks) > 10:
            blocks.append({"type": "context", "elements": [
                {"type": "mrkdwn", "text": "Showing latest 10. View all in Compass UI."}
            ]})
        return {"blocks": blocks[:MAX_BLOCKS]}

    def render_task_detail(self, task: dict) -> dict:
        state = task.get("status", {}).get("state", "UNKNOWN")
        status_msg = ""
        msg_data = task.get("status", {}).get("message", {})
        if isinstance(msg_data, dict):
            parts = msg_data.get("parts", [])
            if parts and isinstance(parts[0], dict):
                status_msg = parts[0].get("text", "")
        task_id = task.get("id", "")
        emoji = _state_emoji(state)
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} Task {task_id}"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Status:*\n{state}"},
            ]},
        ]
        if status_msg:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _truncate(status_msg, MAX_BLOCK_TEXT_LEN)}})
        return {"blocks": blocks}

    def render_input_required(self, question: str, task_id: str) -> dict:
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": "\u2753 Input Required"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Task:*\n{task_id}"},
            ]},
            {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(question, MAX_BLOCK_TEXT_LEN)}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"Reply in this thread or use `/compass resume {task_id} <your answer>`"}
            ]},
        ]
        return {"blocks": blocks}

    def render_task_completed(self, task_id: str, summary: str, links: list[dict] | None = None) -> dict:
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": "\u2705 Task Completed"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Task:*\n{task_id}"},
            ]},
            {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(summary, MAX_BLOCK_TEXT_LEN)}},
        ]
        if links:
            link_texts = [f"<{l['url']}|{l.get('title', 'Link')}>" for l in links[:5]]
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": " | ".join(link_texts)}})
        return {"blocks": blocks}

    def render_task_failed(self, task_id: str, error_summary: str) -> dict:
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": "\u274c Task Failed"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Task:*\n{task_id}"},
            ]},
            {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(error_summary, MAX_BLOCK_TEXT_LEN)}},
        ]
        return {"blocks": blocks}

    def render_help(self) -> dict:
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": "Compass Bot"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "I can help you create and track development and office tasks."}},
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                "*Available commands:*\n"
                "\u2022 `/compass tasks` — List your running tasks\n"
                "\u2022 `/compass task <id>` — View task details\n"
                "\u2022 `/compass resume <id> <text>` — Reply to a task waiting for input\n"
                "\u2022 `/compass help` — Show this help\n"
                "\n_Or just send a message to create a new task._"
            )}},
        ]
        return {"blocks": blocks}

    def render_error(self, message: str) -> dict:
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f":warning: {_truncate(message, MAX_BLOCK_TEXT_LEN)}"}},
        ]
        return {"blocks": blocks}

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


# ---- Helpers ----

def _state_emoji(state: str) -> str:
    mapping = {
        "TASK_STATE_COMPLETED": "\u2705",
        "COMPLETED": "\u2705",
        "TASK_STATE_FAILED": "\u274c",
        "FAILED": "\u274c",
        "TASK_STATE_INPUT_REQUIRED": "\u2753",
        "TASK_STATE_WORKING": "\U0001f504",
        "WORKING": "\U0001f504",
        "ROUTING": "\U0001f504",
        "SUBMITTED": "\U0001f504",
    }
    return mapping.get(state, "\U0001f504")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 30] + "\n\n_...truncated. See Compass UI for full content._"


# Self-register at import time
register_connector("slack", SlackConnector)
