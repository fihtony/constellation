"""Teams Connector — Microsoft Teams Bot Framework adapter for im-gateway.

Implements the unified connector pattern described in
docs/compass-slack-integration-zh.md §3.
"""

from __future__ import annotations

import json
import os
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from im_gateway.connectors import IMConnector, NormalizedMessage
from im_gateway.connectors.registry import register_connector
from im_gateway.connectors.teams import cards
from im_gateway.connectors.teams.normalizer import normalize_text


def _session_mode_for_conversation(conversation: dict) -> str:
    conversation_type = str((conversation or {}).get("conversationType") or "").strip().lower()
    if conversation_type == "personal":
        return "personal"
    if conversation_type in ("groupchat", "group"):
        return "shared-session"
    if conversation_type == "channel":
        return "team-scoped"
    return "personal"


class TeamsConnector(IMConnector):
    """Microsoft Teams connector (Bot Framework Activities → NormalizedMessage)."""

    @property
    def channel_id(self) -> str:
        return "teams"

    @property
    def requires_immediate_ack(self) -> bool:
        return False

    def __init__(self, config: dict):
        self._app_id = config.get("MICROSOFT_APP_ID", "")
        self._app_password = config.get("MICROSOFT_APP_PASSWORD", "")
        self._token_cache: dict[str, tuple[str, float]] = {}
        self._token_lock = threading.Lock()

    @classmethod
    def is_configured(cls, config: dict) -> bool:
        # Teams connector is always loadable; proactive messaging needs creds
        return True

    # ---- Validation ----

    def validate_request(self, headers: dict, body: bytes) -> bool:
        # In production, validate Bot Framework JWT bearer token.
        # For dev / local testing, pass through.
        return True

    # ---- Inbound ----

    def normalize_inbound(self, raw_payload: dict, headers: dict | None = None) -> NormalizedMessage | None:
        activity_type = raw_payload.get("type", "")
        if activity_type == "conversationUpdate":
            return self._handle_conversation_update(raw_payload)
        if activity_type != "message":
            return None

        from_obj = raw_payload.get("from", {})
        user_id = from_obj.get("aadObjectId", "") or from_obj.get("id", "")
        tenant_id = (
            raw_payload.get("channelData", {}).get("tenant", {}).get("id", "")
            or raw_payload.get("conversation", {}).get("tenantId", "")
        )
        conversation = raw_payload.get("conversation", {})
        conversation_id = conversation.get("id", "")
        service_url = raw_payload.get("serviceUrl", "")
        bot_id = (raw_payload.get("recipient", {}) or {}).get("id", "")
        session_mode = _session_mode_for_conversation(conversation)

        # Normalize text
        raw_text = raw_payload.get("text", "")
        text_format = raw_payload.get("textFormat", "plain")
        text = normalize_text(raw_text, text_format)
        if not text:
            return None

        # Parse command
        command, command_args = "", text
        stripped = text.strip()
        if stripped.startswith("/"):
            parts = stripped.split(None, 1)
            command = parts[0].lower()
            command_args = parts[1] if len(parts) > 1 else ""

        return NormalizedMessage(
            channel="teams",
            user_id=user_id,
            workspace_id=tenant_id,
            text=text,
            command=command,
            command_args=command_args,
            session_mode=session_mode,
            reply_target={
                "conversation_id": conversation_id,
                "service_url": service_url,
                "bot_id": bot_id,
            },
            thread_ref=conversation_id,
            raw_payload=raw_payload,
        )

    def _handle_conversation_update(self, activity: dict) -> NormalizedMessage | None:
        """Handle install/uninstall events as special commands."""
        from_obj = activity.get("from", {})
        user_id = from_obj.get("aadObjectId", "") or from_obj.get("id", "")
        tenant_id = (
            activity.get("channelData", {}).get("tenant", {}).get("id", "")
            or activity.get("conversation", {}).get("tenantId", "")
        )
        conversation = activity.get("conversation", {})
        conversation_id = conversation.get("id", "")
        service_url = activity.get("serviceUrl", "")
        bot_id = (activity.get("recipient", {}) or {}).get("id", "")
        recipient_id = bot_id
        session_mode = _session_mode_for_conversation(conversation)

        members_added = activity.get("membersAdded", [])
        members_removed = activity.get("membersRemoved", [])

        for member in members_added:
            if member.get("id") == recipient_id:
                return NormalizedMessage(
                    channel="teams",
                    user_id=user_id,
                    workspace_id=tenant_id,
                    text="",
                    command="__install__",
                    command_args="",
                    session_mode=session_mode,
                    reply_target={
                        "conversation_id": conversation_id,
                        "service_url": service_url,
                        "bot_id": bot_id,
                    },
                    thread_ref=conversation_id,
                    raw_payload=activity,
                )

        for member in members_removed:
            if member.get("id") == recipient_id:
                return NormalizedMessage(
                    channel="teams",
                    user_id=user_id,
                    workspace_id=tenant_id,
                    text="",
                    command="__uninstall__",
                    command_args="",
                    session_mode=session_mode,
                    reply_target={},
                    thread_ref=conversation_id,
                    raw_payload=activity,
                )

        return None

    @staticmethod
    def _normalize_text(text: str | None, text_format: str = "plain") -> str:
        """Backward-compatible static method delegating to normalizer module."""
        return normalize_text(text, text_format)

    # ---- Outbound rendering (Adaptive Cards) ----

    @staticmethod
    def _card_envelope(body: list) -> dict:
        return cards.card_envelope(body)

    def render_task_created(self, task_id: str, summary: str) -> dict:
        return cards.task_created(task_id, summary)

    def render_task_list(self, tasks: list[dict]) -> dict:
        return cards.task_list(tasks)

    def render_task_detail(self, task: dict) -> dict:
        return cards.task_detail(task)

    def render_input_required(self, question: str, task_id: str) -> dict:
        return cards.input_required(question, task_id)

    def render_task_completed(self, task_id: str, summary: str, links: list[dict] | None = None) -> dict:
        return cards.task_completed(task_id, summary, links)

    def render_task_failed(self, task_id: str, error_summary: str) -> dict:
        return cards.task_failed(task_id, error_summary)

    def render_help(self) -> dict:
        return cards.help_message()

    def render_error(self, message: str) -> dict:
        return cards.error_message(message)

    # ---- Proactive messaging ----

    def send_message(self, target: dict, content: dict) -> str:
        service_url = target.get("service_url", "").rstrip("/")
        conversation_id = target.get("conversation_id", "")
        if not service_url or not conversation_id:
            return "error"

        token = self._get_bot_framework_token()
        if not token:
            print("[im-gateway] Teams proactive message skipped (no Bot credentials)")
            return "ok"

        activity = {"type": "message", "attachments": [content]}
        body = json.dumps(activity, ensure_ascii=False).encode("utf-8")
        url = f"{service_url}/v3/conversations/{conversation_id}/activities"
        req = Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=15) as resp:
                resp.read()
            return "ok"
        except HTTPError as err:
            if err.code in (401, 403):
                return "unauthorized"
            if err.code == 429:
                return "rate_limited"
            print(f"[im-gateway] Teams proactive HTTP error {err.code}: {err}")
            return "error"
        except Exception as err:
            print(f"[im-gateway] Teams proactive message failed: {err}")
            return "error"

    def _get_bot_framework_token(self) -> str | None:
        if not self._app_id or not self._app_password:
            return None
        with self._token_lock:
            cached = self._token_cache.get("default")
            if cached and cached[1] > time.time() + 60:
                return cached[0]
            try:
                payload = (
                    "grant_type=client_credentials"
                    f"&client_id={self._app_id}"
                    f"&client_secret={self._app_password}"
                    "&scope=https%3A%2F%2Fapi.botframework.com%2F.default"
                ).encode("utf-8")
                req = Request(
                    "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token",
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                with urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                token = data["access_token"]
                expires_in = int(data.get("expires_in", 3600))
                self._token_cache["default"] = (token, time.time() + expires_in)
                return token
            except Exception as err:
                print(f"[im-gateway] Failed to get Bot Framework token: {err}")
                return None


# Self-register at import time
register_connector("teams", TeamsConnector)
