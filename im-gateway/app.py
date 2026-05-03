"""Unified IM Gateway — routes messages between IM platforms and Compass.

Architecture: single service with pluggable connectors (Teams, Slack, Lark, ...).
See docs/compass-slack-integration-zh.md §3 for design rationale.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import threading
import time
from urllib.parse import urlparse

# Connector imports (self-register on import)
import im_gateway.connectors.teams.connector  # noqa: F401
import im_gateway.connectors.slack.connector  # noqa: F401
from im_gateway.connectors import IMConnector, NormalizedMessage
from im_gateway.connectors.registry import init_connectors, list_connectors
import im_gateway.compass_client as compass_client
from im_gateway.db import GatewayDB

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8070"))
AGENT_ID = os.environ.get("AGENT_ID", "im-gateway")
ADVERTISED_URL = os.environ.get("ADVERTISED_BASE_URL", f"http://im-gateway:{PORT}")
COMPASS_URL = os.environ.get("COMPASS_URL", "http://compass:8080")
MAX_TASKS_PER_USER = int(os.environ.get("MAX_TASKS_PER_USER", "5"))
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "3"))
MAX_MESSAGE_LENGTH = int(os.environ.get("MAX_MESSAGE_LENGTH", "4000"))

db: GatewayDB = None  # type: ignore[assignment]
connectors: list[IMConnector] = []
_connector_map: dict[str, IMConnector] = {}

# In-memory rate limiter: (channel, user_id) -> [timestamps]
_rate_limits: dict[tuple[str, str], list[float]] = {}
_rate_lock = threading.Lock()


def _ensure_db():
    global db
    if db is None:
        db = GatewayDB()
    return db


def _check_rate_limit(channel: str, user_id: str) -> bool:
    now = time.time()
    cutoff = now - 60.0
    key = (channel, user_id)
    with _rate_lock:
        history = _rate_limits.get(key, [])
        history = [ts for ts in history if ts > cutoff]
        if len(history) >= RATE_LIMIT_PER_MINUTE:
            _rate_limits[key] = history
            return False
        history.append(now)
        _rate_limits[key] = history
        return True


def _sanitize_summary(text: str) -> str:
    """Remove internal paths and credentials from outbound summaries."""
    text = re.sub(r"/app/artifacts/[^\s]*", "[artifact-path]", text)
    text = re.sub(r"/app/data/[^\s]*", "[data-path]", text)
    text = re.sub(r"(?i)(password|token|secret|key)\s*=\s*\S+", r"\1=[REDACTED]", text)
    return text


# ---- Core message handling ----

def handle_inbound(msg: NormalizedMessage, connector: IMConnector) -> dict | None:
    """Process a normalized inbound message. Returns platform-native response payload or None."""

    # Special lifecycle commands
    if msg.command == "__install__":
        db.upsert_conversation(msg.channel, msg.user_id, msg.workspace_id, msg.reply_target)
        print(f"[{AGENT_ID}] User installed bot ({msg.channel}:{msg.user_id[:8]}...)")
        return connector.render_help()

    if msg.command == "__uninstall__":
        db.delete_conversation(msg.channel, msg.user_id, msg.workspace_id)
        print(f"[{AGENT_ID}] User uninstalled bot ({msg.channel}:{msg.user_id[:8]}...)")
        return None

    # Update conversation reference
    if msg.user_id and msg.workspace_id and msg.reply_target:
        db.upsert_conversation(msg.channel, msg.user_id, msg.workspace_id, msg.reply_target)

    text = msg.text
    if not text and not msg.command:
        return connector.render_error("Please enter your request.")

    # Message length check
    if len(text) > MAX_MESSAGE_LENGTH:
        return connector.render_error(
            f"Message too long ({len(text)} chars, max {MAX_MESSAGE_LENGTH}). "
            "Please shorten your message or use Compass UI."
        )

    # Route commands (support both "/tasks" and "/compass tasks")
    cmd = msg.command
    args = msg.command_args

    # Normalize /compass sub-commands
    if cmd == "/compass":
        sub_parts = args.split(None, 1)
        if sub_parts:
            cmd = "/" + sub_parts[0].lower()
            args = sub_parts[1] if len(sub_parts) > 1 else ""
        else:
            cmd = "/help"
            args = ""

    if cmd == "/help":
        return connector.render_help()

    if cmd == "/tasks":
        return _handle_tasks_command(msg, connector)

    if cmd == "/task":
        return _handle_task_detail(args.strip(), msg, connector)

    if cmd == "/resume":
        return _handle_resume(args, msg, connector)

    if cmd and cmd.startswith("/"):
        return connector.render_error(f"Unknown command: {cmd}. Use /help to see available commands.")

    # Regular message
    return _handle_new_message(text, msg, connector)


def _handle_tasks_command(msg: NormalizedMessage, connector: IMConnector) -> dict:
    try:
        all_tasks = compass_client.list_tasks(owner_user_id=msg.user_id)
    except Exception as err:
        print(f"[{AGENT_ID}] Failed to list tasks: {err}")
        return connector.render_error("System temporarily unavailable. Please try again later.")

    user_tasks = [t for t in all_tasks if t.get("ownerUserId") in (None, "", msg.user_id)]
    return connector.render_task_list(user_tasks)


def _handle_task_detail(task_id: str, msg: NormalizedMessage, connector: IMConnector) -> dict:
    if not task_id:
        return connector.render_error("Usage: /task <task_id>")

    owner = db.get_task_owner(task_id)
    if owner and owner.get("user_id") != msg.user_id:
        return connector.render_error("You do not have permission to view this task.")

    try:
        data = compass_client.get_task(task_id)
    except Exception as err:
        print(f"[{AGENT_ID}] Failed to get task {task_id}: {err}")
        return connector.render_error("Task not found.")

    task = data.get("task", {})
    if not task:
        return connector.render_error("Task not found.")

    if task.get("ownerUserId") and task.get("ownerUserId") != msg.user_id:
        return connector.render_error("You do not have permission to view this task.")

    return connector.render_task_detail(task)


def _handle_resume(args: str, msg: NormalizedMessage, connector: IMConnector) -> dict:
    parts = args.strip().split(None, 1)
    if not parts:
        return connector.render_error("Usage: /resume <task_id> <your reply>")
    task_id = parts[0]
    reply_text = parts[1] if len(parts) > 1 else ""
    if not reply_text:
        return connector.render_error("Usage: /resume <task_id> <your reply>")

    owner = db.get_task_owner(task_id)
    if owner and owner.get("user_id") != msg.user_id:
        return connector.render_error("You do not have permission to operate this task.")

    try:
        data = compass_client.get_task(task_id)
        task = data.get("task", {})
        state = task.get("status", {}).get("state", "")
        if state != "TASK_STATE_INPUT_REQUIRED":
            return connector.render_error("This task is not currently waiting for input.")
    except Exception:
        return connector.render_error("Task not found.")

    try:
        message = {
            "messageId": f"{msg.channel}-{int(time.time() * 1000)}",
            "role": "ROLE_USER",
            "parts": [{"text": reply_text}],
            "metadata": {
                "ownerUserId": msg.user_id,
                "tenantId": msg.workspace_id,
                "sourceChannel": msg.channel,
            },
        }
        compass_client.resume_task(task_id, message)
        db.update_task_state(task_id, "TASK_STATE_WORKING")
        return connector.render_task_created(task_id, "Input received. Task resuming...")
    except Exception as err:
        print(f"[{AGENT_ID}] Resume failed for {task_id}: {err}")
        return connector.render_error(f"Failed to resume task.")


def _handle_new_message(text: str, msg: NormalizedMessage, connector: IMConnector) -> dict:
    # Check for INPUT_REQUIRED auto-routing
    user_tasks = db.get_user_tasks(msg.channel, msg.user_id, msg.workspace_id)
    ir_tasks = [t for t in user_tasks if t.get("state") == "TASK_STATE_INPUT_REQUIRED"]

    if ir_tasks:
        target_task = ir_tasks[0]  # newest first
        task_id = target_task["task_id"]
        try:
            message = {
                "messageId": f"{msg.channel}-{int(time.time() * 1000)}",
                "role": "ROLE_USER",
                "parts": [{"text": text}],
                "metadata": {
                    "ownerUserId": msg.user_id,
                    "tenantId": msg.workspace_id,
                    "sourceChannel": msg.channel,
                },
            }
            compass_client.resume_task(task_id, message)
            db.update_task_state(task_id, "TASK_STATE_WORKING")
            hint = "Input received. Task resuming..."
            if len(ir_tasks) > 1:
                other_ids = [t["task_id"] for t in ir_tasks[1:]]
                hint += f" Other tasks awaiting input: {', '.join(other_ids)}. Use /resume <id> <text>."
            return connector.render_task_created(task_id, hint)
        except Exception as err:
            print(f"[{AGENT_ID}] Auto-resume failed for {task_id}: {err}")

    # Rate limit
    if not _check_rate_limit(msg.channel, msg.user_id):
        return connector.render_error("Too many requests. Please try again later.")

    # Concurrent task limit
    active = db.count_active_tasks(msg.channel, msg.user_id, msg.workspace_id)
    if active >= MAX_TASKS_PER_USER:
        return connector.render_error(
            f"You currently have {active} running tasks (max {MAX_TASKS_PER_USER}). "
            "Please wait for some to complete before creating new ones."
        )

    # Create new task
    try:
        message = {
            "messageId": f"{msg.channel}-{int(time.time() * 1000)}",
            "role": "ROLE_USER",
            "parts": [{"text": text}],
            "metadata": {
                "ownerUserId": msg.user_id,
                "tenantId": msg.workspace_id,
                "sourceChannel": msg.channel,
            },
        }
        result = compass_client.send_message(message)
        task = result.get("task", {})
        task_id = task.get("id", "")
        if task_id:
            db.add_task_mapping(
                task_id,
                msg.channel,
                msg.user_id,
                msg.workspace_id,
                msg.thread_ref,
                msg.session_mode,
            )
        return connector.render_task_created(task_id, text[:200])
    except Exception as err:
        print(f"[{AGENT_ID}] Task creation failed: {err}")
        return connector.render_error("Task creation failed. Please try again later.")


def _handle_notification(payload: dict) -> None:
    """Handle notification webhook from Compass about task state changes."""
    task_id = payload.get("taskId", "")
    state = payload.get("state", "")
    status_message = payload.get("statusMessage", "")
    owner_user_id = payload.get("ownerUserId", "")
    source_channel = payload.get("sourceChannel", "")
    workspace_id = payload.get("tenantId", "")
    summary = _sanitize_summary(payload.get("summary", "") or "")

    if not task_id or not state:
        return

    db.update_task_state(task_id, state)

    # Try to look up owner from local mapping
    if not owner_user_id or not source_channel:
        owner = db.get_task_owner(task_id)
        if owner:
            owner_user_id = owner.get("user_id", owner_user_id)
            source_channel = owner.get("channel", source_channel)
            workspace_id = owner.get("workspace", workspace_id)

    if not owner_user_id or not source_channel or not workspace_id:
        print(f"[{AGENT_ID}] Cannot send notification for task {task_id}: no owner info")
        return

    # Get connector
    connector = _connector_map.get(source_channel)
    if not connector:
        print(f"[{AGENT_ID}] No connector for channel '{source_channel}'")
        return

    # Get conversation
    conv = db.get_conversation(source_channel, owner_user_id, workspace_id)
    if not conv or not conv.get("is_valid"):
        print(f"[{AGENT_ID}] No valid conversation for user {owner_user_id[:8]}...")
        return

    target = conv.get("target", {})

    # Build notification card
    if state == "TASK_STATE_INPUT_REQUIRED":
        card = connector.render_input_required(status_message, task_id)
    elif state in ("TASK_STATE_COMPLETED", "COMPLETED"):
        card = connector.render_task_completed(task_id, summary or status_message)
    elif state in ("TASK_STATE_FAILED", "FAILED"):
        card = connector.render_task_failed(task_id, _sanitize_summary(status_message))
    else:
        return

    result = connector.send_message(target, card)
    if result == "unauthorized":
        db.mark_conversation_invalid(source_channel, owner_user_id, workspace_id)
    elif result in ("error", "rate_limited"):
        db.increment_failure(source_channel, owner_user_id, workspace_id)


# ---- HTTP Server ----

class IMGatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> tuple[bytes, dict]:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        return raw, parsed

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            self._send_json(200, {
                "status": "ok",
                "service": AGENT_ID,
                "connectors": list_connectors(),
            })
            return

        if path == "/.well-known/agent-card.json":
            card_path = os.path.join(os.path.dirname(__file__), "agent-card.json")
            with open(card_path, encoding="utf-8") as fh:
                card_data = json.load(fh)
            text = json.dumps(card_data).replace("__ADVERTISED_URL__", ADVERTISED_URL)
            self._send_json(200, json.loads(text))
            return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path

        # Teams Bot Framework endpoint
        if path == "/api/messages":
            return self._handle_platform_inbound("teams")

        # Slack Events API endpoint
        if path == "/api/slack/events":
            return self._handle_slack_inbound()

        # Notification webhook (Compass -> Gateway)
        if path == "/api/notifications":
            _, body = self._read_body()
            threading.Thread(target=_handle_notification, args=(body,), daemon=True).start()
            self._send_json(200, {"ok": True})
            return

        self._send_json(404, {"error": "not_found"})

    def _handle_platform_inbound(self, channel_name: str):
        connector = _connector_map.get(channel_name)
        if not connector:
            self._send_json(503, {"error": f"connector '{channel_name}' not available"})
            return

        raw_body, body = self._read_body()
        headers_dict = {k: v for k, v in self.headers.items()}

        if not connector.validate_request(headers_dict, raw_body):
            self._send_json(401, {"error": "invalid_signature"})
            return

        msg = connector.normalize_inbound(body, headers_dict)
        if msg is None:
            self._send_json(200, {})
            return

        # Dedup check
        activity_id = body.get("id", "") or body.get("event_id", "")
        if activity_id and db.check_and_record_activity(activity_id):
            self._send_json(200, {})
            return

        response = handle_inbound(msg, connector)
        if response:
            # Teams: return card in response body
            self._send_json(200, {"type": "message", "attachments": [response]})
        else:
            self._send_json(200, {})

    def _handle_slack_inbound(self):
        connector = _connector_map.get("slack")
        if not connector:
            self._send_json(503, {"error": "slack connector not available"})
            return

        raw_body, body = self._read_body()
        headers_dict = {k: v for k, v in self.headers.items()}

        if not connector.validate_request(headers_dict, raw_body):
            self._send_json(401, {"error": "invalid_signature"})
            return

        # URL verification challenge
        if body.get("type") == "url_verification":
            self._send_json(200, {"challenge": body.get("challenge", "")})
            return

        # ACK immediately (Slack 3-second rule)
        self._send_json(200, {})

        # Process async
        def _async_process():
            msg = connector.normalize_inbound(body, headers_dict)
            if msg is None:
                return

            activity_id = body.get("event_id", "")
            if activity_id and db.check_and_record_activity(activity_id):
                return

            response = handle_inbound(msg, connector)
            if response and msg.reply_target:
                connector.send_message(msg.reply_target, response)

        threading.Thread(target=_async_process, daemon=True).start()

    def log_message(self, fmt, *args):
        line = args[0] if args else ""
        if "/health" in line:
            return
        print(f"[{AGENT_ID}] {line}")


# ---- Startup ----

def _register_notification_target():
    callback_url = f"{ADVERTISED_URL}/api/notifications"
    for attempt in range(5):
        try:
            compass_client.register_notification_target(callback_url)
            print(f"[{AGENT_ID}] Registered notification target: {callback_url}")
            return
        except Exception as err:
            print(f"[{AGENT_ID}] Failed to register notification target (attempt {attempt + 1}): {err}")
            time.sleep(2)
    print(f"[{AGENT_ID}] WARNING: Could not register notification target after 5 attempts")


def _periodic_cleanup():
    while True:
        time.sleep(3600)
        try:
            db.cleanup_old_activities()
            db.cleanup_old_task_mappings()
            print(f"[{AGENT_ID}] Periodic cleanup completed")
        except Exception as err:
            print(f"[{AGENT_ID}] Cleanup error: {err}")


def main():
    global db, connectors, _connector_map
    db = GatewayDB()

    # Build config from environment
    config = dict(os.environ)

    # Initialize connectors
    connectors = init_connectors(config)
    _connector_map = {c.channel_id: c for c in connectors}

    print(f"[{AGENT_ID}] IM Gateway starting on {HOST}:{PORT}")
    print(f"[{AGENT_ID}] Active connectors: {[c.channel_id for c in connectors]}")
    print(f"[{AGENT_ID}] Compass URL: {COMPASS_URL}")

    threading.Thread(target=_register_notification_target, daemon=True).start()
    threading.Thread(target=_periodic_cleanup, daemon=True).start()

    server = ThreadingHTTPServer((HOST, PORT), IMGatewayHandler)
    server.socket.listen(128)
    server.serve_forever()


if __name__ == "__main__":
    main()
