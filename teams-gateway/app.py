"""Teams Gateway — Microsoft Teams Bot adapter for Compass."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import threading
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import compass_client
import cards
from db import GatewayDB
from message_normalizer import normalize_message

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8070"))
AGENT_ID = os.environ.get("AGENT_ID", "teams-gateway")
ADVERTISED_URL = os.environ.get("ADVERTISED_BASE_URL", f"http://teams-gateway:{PORT}")
COMPASS_URL = os.environ.get("COMPASS_URL", "http://compass:8080")
MICROSOFT_APP_ID = os.environ.get("MICROSOFT_APP_ID", "")
MICROSOFT_APP_PASSWORD = os.environ.get("MICROSOFT_APP_PASSWORD", "")
MAX_TASKS_PER_USER = int(os.environ.get("MAX_TASKS_PER_USER", "5"))
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "3"))
MAX_MESSAGE_LENGTH = int(os.environ.get("MAX_MESSAGE_LENGTH", "4000"))

db: GatewayDB = None  # type: ignore  # initialized in main() or test setup


def _ensure_db():
    global db
    if db is None:
        db = GatewayDB()
    return db

# In-memory rate limiter: user_aad_id -> [(timestamp, ...)]
_rate_limits: dict[str, list[float]] = {}
_rate_lock = threading.Lock()


def _check_rate_limit(user_aad_id: str) -> bool:
    """Return True if the user is within rate limits."""
    now = time.time()
    cutoff = now - 60.0
    with _rate_lock:
        history = _rate_limits.get(user_aad_id, [])
        history = [ts for ts in history if ts > cutoff]
        if len(history) >= RATE_LIMIT_PER_MINUTE:
            _rate_limits[user_aad_id] = history
            return False
        history.append(now)
        _rate_limits[user_aad_id] = history
        return True


def _parse_command(text: str) -> tuple[str, str]:
    """Parse /command from text. Returns (command, args) or ('', text)."""
    text = text.strip()
    if not text.startswith("/"):
        return "", text
    parts = text.split(None, 1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    return cmd, args


def _handle_activity(activity: dict) -> dict | None:
    """Process a Bot Framework activity. Returns an Adaptive Card attachment or None."""
    activity_type = activity.get("type", "")
    activity_id = activity.get("id", "")

    # Extract user context
    from_obj = activity.get("from", {})
    user_aad_id = from_obj.get("aadObjectId", "") or from_obj.get("id", "")
    tenant_id = (activity.get("channelData", {}).get("tenant", {}).get("id", "")
                 or activity.get("conversation", {}).get("tenantId", ""))
    conversation_id = activity.get("conversation", {}).get("id", "")
    service_url = activity.get("serviceUrl", "")
    bot_id = (activity.get("recipient", {}) or {}).get("id", "")

    # Handle conversationUpdate (install / uninstall)
    if activity_type == "conversationUpdate":
        return _handle_conversation_update(activity, user_aad_id, tenant_id,
                                           conversation_id, service_url, bot_id)

    if activity_type != "message":
        return None

    # Dedup
    if activity_id and db.check_and_record_activity(activity_id):
        print(f"[{AGENT_ID}] Duplicate activity {activity_id}, ignoring")
        return None  # silent ignore

    # Update conversation reference on every message
    if user_aad_id and tenant_id and conversation_id and service_url:
        db.upsert_conversation_ref(user_aad_id, tenant_id, conversation_id, service_url, bot_id)

    # Normalize message text
    raw_text = activity.get("text", "")
    text_format = activity.get("textFormat", "plain")
    text = normalize_message(raw_text, text_format)

    if not text:
        return cards.error_card("Please enter your request.")

    # Check message length
    if len(text) > MAX_MESSAGE_LENGTH:
        return cards.error_card(
            f"Message too long ({len(text)} chars, max {MAX_MESSAGE_LENGTH}). "
            "Please shorten your message or use Compass UI."
        )

    # Parse commands
    cmd, args = _parse_command(text)

    if cmd == "/help":
        return cards.help_card()

    if cmd == "/tasks":
        return _handle_tasks_command(user_aad_id, tenant_id)

    if cmd == "/task":
        return _handle_task_detail(args.strip(), user_aad_id, tenant_id)

    if cmd == "/resume":
        return _handle_resume(args, user_aad_id, tenant_id)

    if cmd and cmd.startswith("/"):
        return cards.error_card(f"Unknown command: {cmd}. Use /help to see available commands.")

    # Regular message — check for INPUT_REQUIRED tasks or create new task
    return _handle_new_message(text, user_aad_id, tenant_id)


def _handle_conversation_update(activity, user_aad_id, tenant_id,
                                 conversation_id, service_url, bot_id) -> dict | None:
    members_added = activity.get("membersAdded", [])
    members_removed = activity.get("membersRemoved", [])
    recipient_id = (activity.get("recipient", {}) or {}).get("id", "")

    for member in members_added:
        if member.get("id") == recipient_id:
            # Bot was added — store conv ref and send welcome
            if user_aad_id and tenant_id:
                db.upsert_conversation_ref(user_aad_id, tenant_id, conversation_id, service_url, bot_id)
            print(f"[{AGENT_ID}] User installed bot (aad={user_aad_id[:8]}...)")
            return cards.welcome_card()

    for member in members_removed:
        if member.get("id") == recipient_id:
            # Bot was removed
            if user_aad_id and tenant_id:
                db.delete_conversation_ref(user_aad_id, tenant_id)
            print(f"[{AGENT_ID}] User uninstalled bot (aad={user_aad_id[:8]}...)")
            return None

    return None


def _handle_tasks_command(user_aad_id: str, tenant_id: str) -> dict:
    """Handle /tasks command — list user's tasks."""
    try:
        all_tasks = compass_client.list_tasks()
    except Exception as err:
        print(f"[{AGENT_ID}] Failed to list tasks from Compass: {err}")
        return cards.error_card("System temporarily unavailable. Please try again later.")

    # Filter to this user's tasks
    user_tasks = [t for t in all_tasks if t.get("ownerUserId") == user_aad_id]
    return cards.task_list_card(user_tasks)


def _handle_task_detail(task_id: str, user_aad_id: str, tenant_id: str) -> dict:
    """Handle /task <id> command."""
    if not task_id:
        return cards.error_card("Usage: /task <task_id>")

    # Check ownership in local mapping first
    owner = db.get_task_owner(task_id)
    if owner and owner.get("user_aad_id") != user_aad_id:
        return cards.error_card("You do not have permission to view this task.")

    try:
        data = compass_client.get_task(task_id)
    except Exception as err:
        print(f"[{AGENT_ID}] Failed to get task {task_id}: {err}")
        return cards.error_card("Task not found.")

    task = data.get("task", {})
    if not task:
        return cards.error_card("Task not found.")

    # Verify ownership from Compass response
    if task.get("ownerUserId") and task.get("ownerUserId") != user_aad_id:
        return cards.error_card("You do not have permission to view this task.")

    return cards.task_detail_card(task)


def _handle_resume(args: str, user_aad_id: str, tenant_id: str) -> dict:
    """Handle /resume <task_id> <message>."""
    parts = args.strip().split(None, 1)
    if not parts:
        return cards.error_card("Usage: /resume <task_id> <your reply>")
    task_id = parts[0]
    reply_text = parts[1] if len(parts) > 1 else ""
    if not reply_text:
        return cards.error_card("Usage: /resume <task_id> <your reply>")

    # Ownership check
    owner = db.get_task_owner(task_id)
    if owner and owner.get("user_aad_id") != user_aad_id:
        return cards.error_card("You do not have permission to operate this task.")

    # Check task state
    try:
        data = compass_client.get_task(task_id)
        task = data.get("task", {})
        state = task.get("status", {}).get("state", "")
        if state != "TASK_STATE_INPUT_REQUIRED":
            return cards.error_card("This task is not currently waiting for input.")
    except Exception:
        return cards.error_card("Task not found.")

    try:
        message = {
            "messageId": f"teams-{int(time.time()*1000)}",
            "role": "ROLE_USER",
            "parts": [{"text": reply_text}],
            "metadata": {
                "ownerUserId": user_aad_id,
                "tenantId": tenant_id,
                "sourceChannel": "teams",
            },
        }
        compass_client.resume_task(task_id, message)
        db.update_task_state(task_id, "TASK_STATE_WORKING")
        return cards.task_created_card(task_id, "Input received. Task resuming...")
    except Exception as err:
        print(f"[{AGENT_ID}] Resume failed for {task_id}: {err}")
        return cards.error_card(f"Failed to resume task: {err}")


def _handle_new_message(text: str, user_aad_id: str, tenant_id: str) -> dict:
    """Handle a regular text message — check for INPUT_REQUIRED or create new task."""

    # Check if user has exactly one INPUT_REQUIRED task — auto-route reply
    user_tasks = db.get_user_tasks(user_aad_id, tenant_id)
    ir_tasks = [t for t in user_tasks if t.get("last_known_state") == "TASK_STATE_INPUT_REQUIRED"]
    if len(ir_tasks) == 1:
        task_id = ir_tasks[0]["task_id"]
        try:
            message = {
                "messageId": f"teams-{int(time.time()*1000)}",
                "role": "ROLE_USER",
                "parts": [{"text": text}],
                "metadata": {
                    "ownerUserId": user_aad_id,
                    "tenantId": tenant_id,
                    "sourceChannel": "teams",
                },
            }
            compass_client.resume_task(task_id, message)
            db.update_task_state(task_id, "TASK_STATE_WORKING")
            return cards.task_created_card(task_id, "Input received. Task resuming...")
        except Exception as err:
            print(f"[{AGENT_ID}] Auto-resume failed for {task_id}: {err}")
    elif len(ir_tasks) > 1:
        # Multiple INPUT_REQUIRED — route to newest, hint about /resume
        newest = ir_tasks[0]  # already sorted by created_at DESC
        task_id = newest["task_id"]
        try:
            message = {
                "messageId": f"teams-{int(time.time()*1000)}",
                "role": "ROLE_USER",
                "parts": [{"text": text}],
                "metadata": {
                    "ownerUserId": user_aad_id,
                    "tenantId": tenant_id,
                    "sourceChannel": "teams",
                },
            }
            compass_client.resume_task(task_id, message)
            db.update_task_state(task_id, "TASK_STATE_WORKING")
            other_ids = [t["task_id"] for t in ir_tasks[1:]]
            hint = f"Replied to task {task_id}. Other tasks awaiting input: {', '.join(other_ids)}. Use /resume <id> <text> to reply to a specific task."
            return cards.task_created_card(task_id, hint)
        except Exception as err:
            print(f"[{AGENT_ID}] Auto-resume failed for {task_id}: {err}")

    # Rate limit check
    if not _check_rate_limit(user_aad_id):
        return cards.error_card("Too many requests. Please try again later.")

    # Concurrent task limit check
    active = db.count_active_tasks(user_aad_id, tenant_id)
    if active >= MAX_TASKS_PER_USER:
        return cards.error_card(
            f"You currently have {active} running tasks (max {MAX_TASKS_PER_USER}). "
            "Please wait for some to complete before creating new ones."
        )

    # Create new task
    try:
        message = {
            "messageId": f"teams-{int(time.time()*1000)}",
            "role": "ROLE_USER",
            "parts": [{"text": text}],
            "metadata": {
                "ownerUserId": user_aad_id,
                "tenantId": tenant_id,
                "sourceChannel": "teams",
            },
        }
        result = compass_client.send_message(message)
        task = result.get("task", {})
        task_id = task.get("id", "")
        if task_id:
            db.add_task_mapping(task_id, user_aad_id, tenant_id)
        return cards.task_created_card(task_id, text[:200])
    except Exception as err:
        print(f"[{AGENT_ID}] Task creation failed: {err}")
        return cards.error_card(f"Task creation failed. Please try again later.")


def _sanitize_summary(text: str) -> str:
    """Remove internal paths and credentials from outbound summaries (SEC-009)."""
    # Strip internal container paths
    text = re.sub(r"/app/artifacts/[^\s]*", "[artifact-path]", text)
    text = re.sub(r"/app/data/[^\s]*", "[data-path]", text)
    # Strip potential credential patterns (basic)
    text = re.sub(r"(?i)(password|token|secret|key)\s*=\s*\S+", r"\1=[REDACTED]", text)
    return text


# Bot Framework token cache: {tenant_id: (token_str, expires_at)}
_bf_token_cache: dict[str, tuple[str, float]] = {}
_bf_token_lock = threading.Lock()


def _get_bot_framework_token() -> str | None:
    """Obtain a Bot Framework bearer token for sending proactive messages."""
    if not MICROSOFT_APP_ID or not MICROSOFT_APP_PASSWORD:
        return None
    with _bf_token_lock:
        cached = _bf_token_cache.get("default")
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        try:
            payload = (
                "grant_type=client_credentials"
                f"&client_id={MICROSOFT_APP_ID}"
                f"&client_secret={MICROSOFT_APP_PASSWORD}"
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
            _bf_token_cache["default"] = (token, time.time() + expires_in)
            return token
        except Exception as err:
            print(f"[{AGENT_ID}] Failed to get Bot Framework token: {err}")
            return None


def _send_proactive_message(conv_ref: dict, card: dict) -> str:
    """Send a proactive Adaptive Card to a Teams conversation.

    Returns: "ok" | "unauthorized" | "rate_limited" | "error"
    """
    service_url = conv_ref.get("service_url", "").rstrip("/")
    conversation_id = conv_ref.get("conversation_id", "")
    if not service_url or not conversation_id:
        return "error"

    token = _get_bot_framework_token()
    if not token:
        # No credentials configured — log only (dev/test mode)
        print(f"[{AGENT_ID}] Proactive message skipped (no Bot credentials): conv={conversation_id[:16]}...")
        return "ok"

    activity = {
        "type": "message",
        "attachments": [card],
    }
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
        print(f"[{AGENT_ID}] Proactive message HTTP error {err.code}: {err}")
        return "error"
    except Exception as err:
        print(f"[{AGENT_ID}] Proactive message failed: {err}")
        return "error"


def _handle_notification(payload: dict) -> None:
    """Handle a notification webhook from Compass about task state changes."""
    task_id = payload.get("taskId", "")
    state = payload.get("state", "")
    status_message = payload.get("statusMessage", "")
    owner_user_id = payload.get("ownerUserId", "")
    tenant_id = payload.get("tenantId", "")
    summary = _sanitize_summary(payload.get("summary", "") or "")

    if not task_id or not state:
        return

    # Update local task state
    db.update_task_state(task_id, state)

    # If no owner info, try to look up from local mapping
    if not owner_user_id:
        owner = db.get_task_owner(task_id)
        if owner:
            owner_user_id = owner.get("user_aad_id", "")
            tenant_id = owner.get("tenant_id", "")

    if not owner_user_id or not tenant_id:
        print(f"[{AGENT_ID}] Cannot send notification for task {task_id}: no owner info")
        return

    # Get conversation reference
    conv_ref = db.get_conversation_ref(owner_user_id, tenant_id)
    if not conv_ref or not conv_ref.get("is_valid"):
        print(f"[{AGENT_ID}] No valid conversation ref for user {owner_user_id[:8]}...")
        return

    # Build notification card
    if state == "TASK_STATE_INPUT_REQUIRED":
        card = cards.input_required_card(task_id, status_message)
    elif state in ("TASK_STATE_COMPLETED", "COMPLETED"):
        card = cards.completed_card(task_id, summary or status_message)
    elif state in ("TASK_STATE_FAILED", "FAILED"):
        card = cards.failed_card(task_id, _sanitize_summary(status_message))
    else:
        return

    result = _send_proactive_message(conv_ref, card)
    if result == "unauthorized":
        db.mark_conversation_invalid(owner_user_id, tenant_id)
        print(f"[{AGENT_ID}] Conv ref invalidated (403): user={owner_user_id[:8]}...")
    elif result in ("error", "rate_limited"):
        db.increment_failure(owner_user_id, tenant_id)
        print(f"[{AGENT_ID}] Notification failed ({result}): task={task_id} user={owner_user_id[:8]}...")


class TeamsGatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            self._send_json(200, {"status": "ok", "service": AGENT_ID})
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

        # Bot Framework webhook endpoint
        if path == "/api/messages":
            body = self._read_body()
            # In production, validate Bot Framework JWT token here.
            # For MVP / local dev, we process all incoming activities.
            card = _handle_activity(body)
            if card:
                self._send_json(200, {
                    "type": "message",
                    "attachments": [card],
                })
            else:
                self._send_json(200, {})
            return

        # Notification webhook endpoint (called by Compass)
        if path == "/api/notifications":
            body = self._read_body()
            threading.Thread(target=_handle_notification, args=(body,), daemon=True).start()
            self._send_json(200, {"ok": True})
            return

        self._send_json(404, {"error": "not_found"})

    def log_message(self, fmt, *args):
        line = args[0] if args else ""
        if "/health" in line:
            return
        print(f"[{AGENT_ID}] {line}")


def _register_notification_target():
    """Register this Gateway's notification webhook with Compass."""
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
    """Background thread for periodic database cleanup."""
    while True:
        time.sleep(3600)  # every hour
        try:
            db.cleanup_old_activities()
            db.cleanup_old_task_mappings()
            print(f"[{AGENT_ID}] Periodic cleanup completed")
        except Exception as err:
            print(f"[{AGENT_ID}] Cleanup error: {err}")


def main():
    global db
    db = GatewayDB()
    print(f"[{AGENT_ID}] Teams Gateway starting on {HOST}:{PORT}")
    print(f"[{AGENT_ID}] Compass URL: {COMPASS_URL}")

    # Register notification target in background
    threading.Thread(target=_register_notification_target, daemon=True).start()

    # Start periodic cleanup
    threading.Thread(target=_periodic_cleanup, daemon=True).start()

    server = ThreadingHTTPServer((HOST, PORT), TeamsGatewayHandler)
    server.socket.listen(128)
    server.serve_forever()


if __name__ == "__main__":
    main()
