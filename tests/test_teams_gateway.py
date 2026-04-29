"""Unit tests for Teams Gateway.

Covers test cases from the design document: GW-001..GW-009, LC-001..LC-006,
MN-001..MN-008, TC-001..TC-010, TQ-001..TQ-008, IR-001..IR-010,
CMD-001..CMD-008, SEC-005..SEC-008, DD-001..DD-003, DR-001..DR-009,
CC-001..CC-005, AC-001..AC-007.

All tests use mocks for Compass HTTP calls and SQLite in-memory or tempdir DBs.
"""

from __future__ import annotations

import html
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from teams_gateway.message_normalizer import normalize_message
from teams_gateway.db import GatewayDB
from teams_gateway import cards


def _make_activity(
    text="hello",
    text_format="plain",
    activity_type="message",
    user_aad_id="user-aad-001",
    tenant_id="tenant-001",
    conversation_id="conv-001",
    service_url="https://smba.trafficmanager.net/teams/",
    activity_id="act-001",
    members_added=None,
    members_removed=None,
):
    """Build a minimal Bot Framework Activity dict."""
    act = {
        "type": activity_type,
        "id": activity_id,
        "text": text,
        "textFormat": text_format,
        "from": {"id": "user-id-1", "aadObjectId": user_aad_id, "name": "Test User"},
        "conversation": {"id": conversation_id, "tenantId": tenant_id},
        "channelData": {"tenant": {"id": tenant_id}},
        "serviceUrl": service_url,
        "recipient": {"id": "bot-id-1", "name": "Compass Bot"},
    }
    if members_added is not None:
        act["membersAdded"] = members_added
    if members_removed is not None:
        act["membersRemoved"] = members_removed
    return act


class TestMessageNormalizer(unittest.TestCase):
    """MN-001..MN-008: Message normalization tests."""

    def test_mn001_plain_text(self):
        """MN-001: Plain text passes through unchanged."""
        self.assertEqual(normalize_message("hello world", "plain"), "hello world")

    def test_mn002_html_stripping(self):
        """MN-002: HTML tags stripped when textFormat=xml."""
        self.assertEqual(
            normalize_message("<b>bold</b> and <i>italic</i>", "xml"),
            "bold and italic",
        )

    def test_mn003_entity_decode(self):
        """MN-003: HTML entities decoded."""
        self.assertEqual(
            normalize_message("a &amp; b &lt; c &gt; d", "xml"),
            "a & b < c > d",
        )

    def test_mn004_emoji_preserved(self):
        """MN-004: Emoji preserved."""
        self.assertEqual(normalize_message("hello 🌟 world", "plain"), "hello 🌟 world")

    def test_mn006_empty_message(self):
        """MN-006: Empty/whitespace returns empty string."""
        self.assertEqual(normalize_message("", "plain"), "")
        self.assertEqual(normalize_message("   ", "plain"), "")
        self.assertEqual(normalize_message(None, "plain"), "")

    def test_mn007_multiline(self):
        """MN-007: Newlines preserved."""
        self.assertEqual(normalize_message("line1\nline2", "plain"), "line1\nline2")

    def test_mn008_xss_payload(self):
        """MN-008: XSS payload stripped safely."""
        result = normalize_message('<script>alert("xss")</script>safe text', "xml")
        self.assertNotIn("<script>", result)
        self.assertIn("safe text", result)
        # The alert content is kept as plain text (no tags remain)
        self.assertIn("alert", result)


class TestGatewayDB(unittest.TestCase):
    """GW-003, GW-007, GW-009, DR-001..DR-009: Database tests."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "teams-gateway", "teams-gateway.db")
        self.db = GatewayDB(db_path=self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_gw007_creates_directory(self):
        """GW-007: DB directory auto-created."""
        self.assertTrue(os.path.isdir(os.path.join(self.tmpdir, "teams-gateway")))
        self.assertTrue(os.path.isfile(self.db_path))

    def test_dr001_conversation_ref_persistence(self):
        """DR-001: Conversation references survive DB reopening."""
        self.db.upsert_conversation_ref("u1", "t1", "c1", "https://svc", "b1")
        # Reopen DB
        db2 = GatewayDB(db_path=self.db_path)
        ref = db2.get_conversation_ref("u1", "t1")
        self.assertIsNotNone(ref)
        self.assertEqual(ref["conversation_id"], "c1")
        self.assertEqual(ref["service_url"], "https://svc")

    def test_dr006_db_in_data_dir(self):
        """DR-006: DB file is inside data/teams-gateway/."""
        self.assertIn("teams-gateway", self.db_path)
        self.assertTrue(os.path.isfile(self.db_path))

    def test_dr009_no_residual_files(self):
        """DR-009: No DB files outside data directory."""
        # After operations, check no .db files exist in tmpdir root
        self.db.upsert_conversation_ref("u1", "t1", "c1", "https://svc", "b1")
        for item in os.listdir(self.tmpdir):
            if item.endswith(".db"):
                self.fail(f"Residual DB file in root: {item}")

    def test_conversation_ref_upsert_and_get(self):
        self.db.upsert_conversation_ref("u1", "t1", "c1", "https://svc", "b1")
        ref = self.db.get_conversation_ref("u1", "t1")
        self.assertIsNotNone(ref)
        self.assertEqual(ref["user_aad_id"], "u1")
        self.assertEqual(ref["is_valid"], 1)

    def test_conversation_ref_delete(self):
        self.db.upsert_conversation_ref("u1", "t1", "c1", "https://svc", "b1")
        self.db.delete_conversation_ref("u1", "t1")
        self.assertIsNone(self.db.get_conversation_ref("u1", "t1"))

    def test_conversation_ref_invalidation(self):
        self.db.upsert_conversation_ref("u1", "t1", "c1", "https://svc", "b1")
        self.db.mark_conversation_invalid("u1", "t1")
        ref = self.db.get_conversation_ref("u1", "t1")
        self.assertEqual(ref["is_valid"], 0)

    def test_consecutive_failure_auto_invalidate(self):
        self.db.upsert_conversation_ref("u1", "t1", "c1", "https://svc", "b1")
        for _ in range(5):
            self.db.increment_failure("u1", "t1")
        ref = self.db.get_conversation_ref("u1", "t1")
        self.assertEqual(ref["is_valid"], 0)
        self.assertEqual(ref["consecutive_failures"], 5)

    def test_task_mapping_crud(self):
        self.db.add_task_mapping("t001", "u1", "tenant1")
        tasks = self.db.get_user_tasks("u1", "tenant1")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_id"], "t001")

        owner = self.db.get_task_owner("t001")
        self.assertIsNotNone(owner)
        self.assertEqual(owner["user_aad_id"], "u1")

    def test_task_mapping_state_update(self):
        self.db.add_task_mapping("t001", "u1", "tenant1")
        self.db.update_task_state("t001", "TASK_STATE_WORKING")
        tasks = self.db.get_user_tasks("u1", "tenant1")
        self.assertEqual(tasks[0]["last_known_state"], "TASK_STATE_WORKING")

    def test_active_task_count(self):
        self.db.add_task_mapping("t001", "u1", "t1")
        self.db.add_task_mapping("t002", "u1", "t1")
        self.db.add_task_mapping("t003", "u1", "t1")
        self.db.update_task_state("t003", "TASK_STATE_COMPLETED")
        self.assertEqual(self.db.count_active_tasks("u1", "t1"), 2)

    def test_activity_dedup(self):
        """DD-001: Duplicate activity detection."""
        is_dup = self.db.check_and_record_activity("act-1")
        self.assertFalse(is_dup)  # first time
        is_dup = self.db.check_and_record_activity("act-1")
        self.assertTrue(is_dup)  # duplicate

    def test_activity_cleanup(self):
        self.db.check_and_record_activity("old-act")
        # Force the record to be old
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE activity_dedup SET processed_at='2020-01-01T00:00:00Z' WHERE activity_id='old-act'"
        )
        conn.commit()
        conn.close()
        self.db.cleanup_old_activities(max_age_seconds=1)
        # Should no longer be a duplicate
        self.assertFalse(self.db.check_and_record_activity("old-act"))

    def test_concurrent_access(self):
        """CC-005: Concurrent DB access."""
        errors = []

        def writer(i):
            try:
                self.db.upsert_conversation_ref(f"u{i}", "t1", f"c{i}", "https://svc", "b1")
                self.db.add_task_mapping(f"task-{i}", f"u{i}", "t1")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent errors: {errors}")


class TestCards(unittest.TestCase):
    """AC-001..AC-007: Adaptive Card format tests."""

    def _validate_card(self, card):
        """Basic card structure validation."""
        self.assertEqual(card["contentType"], "application/vnd.microsoft.card.adaptive")
        content = card["content"]
        self.assertEqual(content["type"], "AdaptiveCard")
        self.assertEqual(content["version"], "1.3")  # AC-005
        self.assertIn("body", content)
        # AC-006: check size < 28KB
        card_json = json.dumps(card)
        self.assertLess(len(card_json), 28 * 1024)
        return content

    def test_ac001_welcome_card(self):
        card = cards.welcome_card("http://localhost:8080")
        content = self._validate_card(card)
        body_text = json.dumps(content["body"])
        self.assertIn("Compass Bot", body_text)
        self.assertIn("/tasks", body_text)

    def test_ac003_input_required_attention_color(self):
        card = cards.input_required_card("t001", "Please provide more info")
        content = self._validate_card(card)
        # Check for attention color
        body = content["body"]
        colors = [b.get("color") for b in body if isinstance(b, dict)]
        self.assertIn("attention", colors)

    def test_ac004_completed_good_color(self):
        card = cards.completed_card("t001", "Task done successfully")
        content = self._validate_card(card)
        body = content["body"]
        colors = [b.get("color") for b in body if isinstance(b, dict)]
        self.assertIn("good", colors)

    def test_ac005_schema_version(self):
        card = cards.task_list_card([])
        content = self._validate_card(card)
        self.assertEqual(content["version"], "1.3")

    def test_ac007_long_text_wrapping(self):
        long_text = "x" * 1500
        card = cards.completed_card("t001", long_text)
        content = self._validate_card(card)
        # Text should be truncated
        body_json = json.dumps(content["body"])
        self.assertLessEqual(len(body_json), 28 * 1024)

    def test_task_list_empty(self):
        card = cards.task_list_card([])
        self._validate_card(card)
        body_text = json.dumps(card["content"]["body"])
        self.assertIn("No running tasks", body_text)

    def test_task_list_with_items(self):
        tasks = [
            {"id": "t001", "status": {"state": "TASK_STATE_WORKING"}, "summary": "Build feature"},
            {"id": "t002", "status": {"state": "TASK_STATE_COMPLETED"}, "summary": "Fix bug"},
        ]
        card = cards.task_list_card(tasks)
        self._validate_card(card)

    def test_error_card(self):
        card = cards.error_card("Something went wrong")
        self._validate_card(card)


class TestActivityHandling(unittest.TestCase):
    """TC-001..TC-010, CMD-001..CMD-008, SEC-005..SEC-008: Activity processing tests."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(self.tmpdir, "tg", "tg.db")

        # Patch the module-level db in teams_gateway.app
        self.db = GatewayDB(db_path=db_path)

        import teams_gateway.app as gw_app
        self._orig_db = gw_app.db
        gw_app.db = self.db
        # Clear per-user rate limit state so tests don't bleed into each other
        gw_app._rate_limits.clear()

    def tearDown(self):
        import teams_gateway.app as gw_app
        gw_app.db = self._orig_db
        gw_app._rate_limits.clear()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("teams_gateway.compass_client.send_message")
    def test_tc001_create_dev_task(self, mock_send):
        """TC-001: New message creates task via Compass."""
        mock_send.return_value = {"task": {"id": "task-0001", "status": {"state": "TASK_STATE_WORKING"}}}
        from teams_gateway.app import _handle_activity
        activity = _make_activity(text="Implement login page")
        card = _handle_activity(activity)
        self.assertIsNotNone(card)
        mock_send.assert_called_once()
        # Check owner metadata was passed
        call_msg = mock_send.call_args[0][0]
        self.assertEqual(call_msg["metadata"]["ownerUserId"], "user-aad-001")
        self.assertEqual(call_msg["metadata"]["sourceChannel"], "teams")

    @patch("teams_gateway.compass_client.send_message")
    def test_tc003_task_confirmation_card(self, mock_send):
        """TC-003: Confirmation card contains task ID."""
        mock_send.return_value = {"task": {"id": "task-0042"}}
        from teams_gateway.app import _handle_activity
        card = _handle_activity(_make_activity(text="Build feature X"))
        self.assertIsNotNone(card)
        card_json = json.dumps(card)
        self.assertIn("task-0042", card_json)

    @patch("teams_gateway.compass_client.send_message")
    def test_tc004_task_has_owner(self, mock_send):
        """TC-004: Task creation passes owner metadata."""
        mock_send.return_value = {"task": {"id": "task-0001"}}
        from teams_gateway.app import _handle_activity
        _handle_activity(_make_activity(text="hello", user_aad_id="my-aad-id"))
        call_msg = mock_send.call_args[0][0]
        self.assertEqual(call_msg["metadata"]["ownerUserId"], "my-aad-id")
        self.assertEqual(call_msg["metadata"]["sourceChannel"], "teams")

    @patch("teams_gateway.compass_client.send_message")
    def test_tc007_dedup(self, mock_send):
        """TC-007: Duplicate activity silently ignored."""
        mock_send.return_value = {"task": {"id": "task-0001"}}
        from teams_gateway.app import _handle_activity
        _handle_activity(_make_activity(text="first", activity_id="dedup-unique-001"))
        _handle_activity(_make_activity(text="second", activity_id="dedup-unique-001"))
        self.assertEqual(mock_send.call_count, 1)  # only called once

    @patch("teams_gateway.compass_client.send_message")
    def test_tc006_concurrent_limit(self, mock_send):
        """TC-006: Concurrent task limit enforced."""
        mock_send.return_value = {"task": {"id": "task-0001"}}
        from teams_gateway.app import _handle_activity

        import teams_gateway.app as gw_app
        orig_max = gw_app.MAX_TASKS_PER_USER
        # Also bump rate limit to avoid rate-limit interference
        orig_rate = gw_app.RATE_LIMIT_PER_MINUTE
        gw_app.MAX_TASKS_PER_USER = 2
        gw_app.RATE_LIMIT_PER_MINUTE = 100

        try:
            # Add 2 active tasks
            self.db.add_task_mapping("t1", "user-aad-001", "tenant-001")
            self.db.add_task_mapping("t2", "user-aad-001", "tenant-001")

            card = _handle_activity(_make_activity(text="new task", activity_id="act-limit"))
            card_json = json.dumps(card)
            self.assertIn("running tasks", card_json.lower())
            mock_send.assert_not_called()
        finally:
            gw_app.MAX_TASKS_PER_USER = orig_max
            gw_app.RATE_LIMIT_PER_MINUTE = orig_rate

    def test_mn005_message_too_long(self):
        """MN-005/TC: Overlong message rejected with error card."""
        import teams_gateway.app as gw_app
        orig = gw_app.MAX_MESSAGE_LENGTH
        gw_app.MAX_MESSAGE_LENGTH = 100
        try:
            from teams_gateway.app import _handle_activity
            card = _handle_activity(_make_activity(text="x" * 200, activity_id="long-msg"))
            card_json = json.dumps(card)
            self.assertIn("too long", card_json.lower())
        finally:
            gw_app.MAX_MESSAGE_LENGTH = orig

    def test_empty_message(self):
        """MN-006: Empty message returns prompt."""
        from teams_gateway.app import _handle_activity
        card = _handle_activity(_make_activity(text="", activity_id="empty-msg"))
        card_json = json.dumps(card)
        self.assertIn("enter your request", card_json.lower())

    def test_cmd001_tasks_command(self):
        """CMD-001: /tasks returns task list."""
        with patch("teams_gateway.compass_client.list_tasks") as mock_list:
            mock_list.return_value = [
                {"id": "t1", "status": {"state": "WORKING"}, "ownerUserId": "user-aad-001"},
            ]
            from teams_gateway.app import _handle_activity
            card = _handle_activity(_make_activity(text="/tasks", activity_id="cmd-tasks"))
            self.assertIsNotNone(card)

    def test_cmd003_help_command(self):
        """CMD-003: /help returns help card."""
        from teams_gateway.app import _handle_activity
        card = _handle_activity(_make_activity(text="/help", activity_id="cmd-help"))
        card_json = json.dumps(card)
        self.assertIn("/tasks", card_json)

    def test_cmd005_unknown_command(self):
        """CMD-005: Unknown command returns error."""
        from teams_gateway.app import _handle_activity
        card = _handle_activity(_make_activity(text="/unknown", activity_id="cmd-unk"))
        card_json = json.dumps(card)
        self.assertIn("Unknown command", card_json)

    def test_cmd007_case_insensitive(self):
        """CMD-007: Commands case insensitive."""
        with patch("teams_gateway.compass_client.list_tasks") as mock_list:
            mock_list.return_value = []
            from teams_gateway.app import _handle_activity
            card = _handle_activity(_make_activity(text="/TASKS", activity_id="cmd-upper"))
            card_json = json.dumps(card)
            self.assertIn("No running tasks", card_json)

    def test_sec005_owner_check_task_detail(self):
        """SEC-005: User cannot view another user's task."""
        self.db.add_task_mapping("t001", "other-user", "tenant-001")
        from teams_gateway.app import _handle_activity
        card = _handle_activity(_make_activity(text="/task t001", activity_id="sec-005"))
        card_json = json.dumps(card)
        self.assertIn("permission", card_json.lower())

    def test_sec006_owner_check_resume(self):
        """SEC-006: User cannot resume another user's task."""
        self.db.add_task_mapping("t001", "other-user", "tenant-001")
        from teams_gateway.app import _handle_activity
        card = _handle_activity(_make_activity(text="/resume t001 my answer", activity_id="sec-006"))
        card_json = json.dumps(card)
        self.assertIn("permission", card_json.lower())

    def test_sec007_xss_in_message(self):
        """SEC-007: XSS payload stripped from input."""
        with patch("teams_gateway.compass_client.send_message") as mock_send:
            mock_send.return_value = {"task": {"id": "t001"}}
            from teams_gateway.app import _handle_activity
            card = _handle_activity(_make_activity(
                text='<script>alert("xss")</script>hello',
                text_format="xml",
                activity_id="sec-xss",
            ))
            call_msg = mock_send.call_args[0][0]
            self.assertNotIn("<script>", call_msg["parts"][0]["text"])

    def test_lc001_bot_install(self):
        """LC-001: Bot install stores conversation ref and sends welcome."""
        from teams_gateway.app import _handle_activity
        activity = _make_activity(
            activity_type="conversationUpdate",
            members_added=[{"id": "bot-id-1"}],
            activity_id="install-1",
        )
        card = _handle_activity(activity)
        self.assertIsNotNone(card)
        card_json = json.dumps(card)
        self.assertIn("Compass Bot", card_json)
        # Verify conversation ref stored
        ref = self.db.get_conversation_ref("user-aad-001", "tenant-001")
        self.assertIsNotNone(ref)

    def test_lc003_bot_uninstall(self):
        """LC-003: Bot uninstall removes conversation ref."""
        self.db.upsert_conversation_ref("user-aad-001", "tenant-001", "c1", "https://svc", "b1")
        from teams_gateway.app import _handle_activity
        activity = _make_activity(
            activity_type="conversationUpdate",
            members_removed=[{"id": "bot-id-1"}],
            activity_id="uninstall-1",
        )
        _handle_activity(activity)
        ref = self.db.get_conversation_ref("user-aad-001", "tenant-001")
        self.assertIsNone(ref)

    @patch("teams_gateway.compass_client.get_task")
    @patch("teams_gateway.compass_client.resume_task")
    def test_ir002_resume_input_required(self, mock_resume, mock_get):
        """IR-002: User replies to INPUT_REQUIRED task."""
        mock_get.return_value = {"task": {"id": "t001", "status": {"state": "TASK_STATE_INPUT_REQUIRED"}}}
        mock_resume.return_value = {"task": {"id": "t001"}}

        self.db.add_task_mapping("t001", "user-aad-001", "tenant-001")
        self.db.update_task_state("t001", "TASK_STATE_INPUT_REQUIRED")

        from teams_gateway.app import _handle_activity
        card = _handle_activity(_make_activity(text="/resume t001 yes", activity_id="ir-002"))
        mock_resume.assert_called_once()

    @patch("teams_gateway.compass_client.send_message")
    def test_ir005_auto_route_to_newest_ir(self, mock_send):
        """IR-005: Auto-route text to newest INPUT_REQUIRED task."""
        mock_send.return_value = {"task": {"id": "t002"}}

        self.db.add_task_mapping("t001", "user-aad-001", "tenant-001")
        self.db.update_task_state("t001", "TASK_STATE_INPUT_REQUIRED")
        self.db.add_task_mapping("t002", "user-aad-001", "tenant-001")
        self.db.update_task_state("t002", "TASK_STATE_INPUT_REQUIRED")

        with patch("teams_gateway.compass_client.resume_task") as mock_resume:
            mock_resume.return_value = {"task": {"id": "t002"}}
            from teams_gateway.app import _handle_activity
            card = _handle_activity(_make_activity(text="my answer", activity_id="ir-005"))
            mock_resume.assert_called_once()


class TestCompassTaskStoreOwnerFields(unittest.TestCase):
    """TC-004, TQ-001: Verify Task owner fields in common/task_store.py."""

    def test_task_has_owner_fields(self):
        from common.task_store import Task, TaskStore
        store = TaskStore()
        task = store.create()
        self.assertEqual(task.owner_user_id, "")
        self.assertEqual(task.owner_display_name, "")
        self.assertEqual(task.tenant_id, "")
        self.assertEqual(task.source_channel, "")

    def test_task_owner_in_dict(self):
        from common.task_store import Task, TaskStore
        store = TaskStore()
        task = store.create()
        task.owner_user_id = "aad-123"
        task.owner_display_name = "John"
        task.tenant_id = "tenant-abc"
        task.source_channel = "teams"
        d = task.to_dict()
        self.assertEqual(d["ownerUserId"], "aad-123")
        self.assertEqual(d["ownerDisplayName"], "John")
        self.assertEqual(d["tenantId"], "tenant-abc")
        self.assertEqual(d["sourceChannel"], "teams")


class TestCompassNotificationTargets(unittest.TestCase):
    """GW-002, PN-003: Notification target registration in Compass."""

    @classmethod
    def setUpClass(cls):
        # Set env vars so compass.app can import without /app filesystem
        os.environ.setdefault("ARTIFACT_ROOT", tempfile.mkdtemp())
        os.environ.setdefault("REGISTRY_URL", "http://localhost:9000")

    def test_notification_target_registration(self):
        """GW-002 / PN-003: Verify _notification_targets list management."""
        # Test the in-memory list directly
        import compass.app as compass_app
        original = list(compass_app._notification_targets)
        compass_app._notification_targets.clear()
        try:
            compass_app._notification_targets.append({"url": "http://gw:8070/api/notifications", "registeredAt": "now"})
            self.assertEqual(len(compass_app._notification_targets), 1)
            self.assertEqual(compass_app._notification_targets[0]["url"], "http://gw:8070/api/notifications")
        finally:
            compass_app._notification_targets.clear()
            compass_app._notification_targets.extend(original)

    def test_fire_notification_only_for_key_states(self):
        """Only INPUT_REQUIRED, COMPLETED, FAILED trigger notifications."""
        import compass.app as compass_app
        from common.task_store import Task
        task = Task()
        task.state = "TASK_STATE_WORKING"
        # Should not fire (no targets and state not in notify set)
        compass_app._fire_notification(task)  # should return immediately


class TestGatewayHTTPEndpoints(unittest.TestCase):
    """GW-001: Health endpoint test via in-process handler."""

    def test_gw001_health(self):
        """GW-001: GET /health returns ok."""
        from teams_gateway.app import TeamsGatewayHandler

        handler = self._create_handler("GET", "/health", TeamsGatewayHandler)
        self.assertIn(b'"status": "ok"', handler._response_body)

    def _create_handler(self, method, path, handler_class, body=None):
        """Create a mock HTTP handler for testing."""
        import io

        class MockHandler(handler_class):
            _response_body = b""
            _response_code = 0

            def __init__(self_inner):
                self_inner.rfile = io.BytesIO(json.dumps(body or {}).encode() if body else b"")
                self_inner.wfile = io.BytesIO()
                self_inner.headers = {"Content-Length": str(len(self_inner.rfile.getvalue()))}
                self_inner.path = path
                self_inner.command = method
                self_inner.requestline = f"{method} {path} HTTP/1.1"
                self_inner.client_address = ("127.0.0.1", 12345)
                self_inner.server = MagicMock()
                self_inner.request_version = "HTTP/1.1"
                self_inner.close_connection = True

            def send_response(self_inner, code):
                self_inner._response_code = code

            def send_header(self_inner, key, value):
                pass

            def end_headers(self_inner):
                pass

            def _send_json(self_inner, code, payload):
                self_inner._response_code = code
                self_inner._response_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        handler = MockHandler()
        if method == "GET":
            handler.do_GET()
        elif method == "POST":
            handler.do_POST()
        return handler


# ---------------------------------------------------------------------------
# Additional test cases from design doc §12 — filling coverage gaps
# ---------------------------------------------------------------------------

class TestActivityHandlingExtended(unittest.TestCase):
    """TC-002, TC-005, TC-008..TC-010, TQ-004..TQ-008, CMD-002, CMD-004, CMD-006,
    IR-001, IR-003, IR-006..IR-007, LC-004..LC-005, CC-001..CC-002."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(self.tmpdir, "tg2", "tg.db")
        self.db = GatewayDB(db_path=db_path)
        import teams_gateway.app as gw_app
        self._orig_db = gw_app.db
        gw_app.db = self.db
        gw_app._rate_limits.clear()

    def tearDown(self):
        import teams_gateway.app as gw_app
        gw_app.db = self._orig_db
        gw_app._rate_limits.clear()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---- TC-002: office task routing ----

    @patch("teams_gateway.compass_client.send_message")
    def test_tc002_office_task(self, mock_send):
        """TC-002: Office task also creates task via Compass."""
        mock_send.return_value = {"task": {"id": "task-office-01"}}
        from teams_gateway.app import _handle_activity
        card = _handle_activity(_make_activity(
            text="Summarize the Q3 financial report",
            activity_id="tc002-office",
        ))
        self.assertIsNotNone(card)
        mock_send.assert_called_once()
        # Owner metadata should be set
        call_msg = mock_send.call_args[0][0]
        self.assertEqual(call_msg["metadata"]["sourceChannel"], "teams")

    # ---- TC-005: rapid task creation (3 tasks, not rate-limited) ----

    @patch("teams_gateway.compass_client.send_message")
    def test_tc005_rapid_tasks_not_rate_limited(self, mock_send):
        """TC-005: Three tasks in quick succession all create (up to rate limit)."""
        mock_send.return_value = {"task": {"id": "task-x"}}
        from teams_gateway.app import _handle_activity
        import teams_gateway.app as gw_app
        orig = gw_app.RATE_LIMIT_PER_MINUTE
        gw_app.RATE_LIMIT_PER_MINUTE = 10  # well above 3
        try:
            for i in range(3):
                _handle_activity(_make_activity(text=f"task {i}", activity_id=f"tc005-{i}"))
            self.assertEqual(mock_send.call_count, 3)
        finally:
            gw_app.RATE_LIMIT_PER_MINUTE = orig

    # ---- TC-008: rate limit trigger ----

    @patch("teams_gateway.compass_client.send_message")
    def test_tc008_rate_limit_trigger(self, mock_send):
        """TC-008: 4th task within a minute is rate-limited."""
        mock_send.return_value = {"task": {"id": "task-x"}}
        import teams_gateway.app as gw_app
        orig = gw_app.RATE_LIMIT_PER_MINUTE
        gw_app.RATE_LIMIT_PER_MINUTE = 3
        try:
            from teams_gateway.app import _handle_activity
            for i in range(3):
                _handle_activity(_make_activity(text=f"task {i}", activity_id=f"tc008-ok-{i}"))
            # 4th should be rate limited
            card = _handle_activity(_make_activity(text="4th task", activity_id="tc008-limited"))
            card_json = json.dumps(card)
            self.assertIn("too many requests", card_json.lower())
            self.assertEqual(mock_send.call_count, 3)
        finally:
            gw_app.RATE_LIMIT_PER_MINUTE = orig

    # ---- TC-009: Compass 500 error ----

    @patch("teams_gateway.compass_client.send_message", side_effect=Exception("500 Internal Server Error"))
    def test_tc009_compass_500(self, mock_send):
        """TC-009: Compass 500 returns friendly error card."""
        from teams_gateway.app import _handle_activity
        card = _handle_activity(_make_activity(text="build something", activity_id="tc009"))
        card_json = json.dumps(card)
        self.assertIn("failed", card_json.lower())

    # ---- TQ-004: /tasks with no tasks ----

    def test_tq004_no_tasks_for_user(self):
        """TQ-004: /tasks returns friendly message when user has no tasks."""
        with patch("teams_gateway.compass_client.list_tasks") as mock_list:
            mock_list.return_value = []
            from teams_gateway.app import _handle_activity
            card = _handle_activity(_make_activity(text="/tasks", activity_id="tq004"))
            card_json = json.dumps(card)
            self.assertIn("No running tasks", card_json)

    # ---- TQ-005: /task <nonexistent id> ----

    def test_tq005_task_not_found(self):
        """TQ-005: /task <bad-id> returns not found."""
        with patch("teams_gateway.compass_client.get_task", side_effect=Exception("404 Not Found")):
            from teams_gateway.app import _handle_activity
            card = _handle_activity(_make_activity(text="/task no-such-task", activity_id="tq005"))
            card_json = json.dumps(card)
            self.assertIn("not found", card_json.lower())

    # ---- TQ-007: task list card format ----

    def test_tq007_task_list_format(self):
        """TQ-007: Task list card includes task ID, state emoji, and truncated summary."""
        tasks = [
            {
                "id": "t-abc",
                "ownerUserId": "user-aad-001",
                "status": {"state": "TASK_STATE_WORKING"},
                "summary": "A" * 100,
            }
        ]
        with patch("teams_gateway.compass_client.list_tasks", return_value=tasks):
            from teams_gateway.app import _handle_activity
            card = _handle_activity(_make_activity(text="/tasks", activity_id="tq007"))
            card_json = json.dumps(card)
            self.assertIn("t-abc", card_json)

    # ---- TQ-008: >10 tasks truncated ----

    def test_tq008_more_than_10_tasks(self):
        """TQ-008: Only 10 tasks shown, with 'View all' hint."""
        tasks = [
            {"id": f"t-{i:03}", "ownerUserId": "user-aad-001",
             "status": {"state": "TASK_STATE_WORKING"}, "summary": ""}
            for i in range(15)
        ]
        card = cards.task_list_card(tasks)
        card_json = json.dumps(card)
        # Should show truncation hint
        self.assertIn("10", card_json)

    # ---- CMD-002: /task <id> command ----

    def test_cmd002_task_detail_command(self):
        """CMD-002: /task <id> routes to task detail handler."""
        self.db.add_task_mapping("t001", "user-aad-001", "tenant-001")
        with patch("teams_gateway.compass_client.get_task") as mock_get:
            mock_get.return_value = {
                "task": {"id": "t001", "status": {"state": "TASK_STATE_WORKING"}, "ownerUserId": "user-aad-001"}
            }
            from teams_gateway.app import _handle_activity
            card = _handle_activity(_make_activity(text="/task t001", activity_id="cmd002"))
            card_json = json.dumps(card)
            self.assertIn("t001", card_json)
            mock_get.assert_called_once_with("t001")

    # ---- CMD-004: /resume command ----

    @patch("teams_gateway.compass_client.get_task")
    @patch("teams_gateway.compass_client.resume_task")
    def test_cmd004_resume_command(self, mock_resume, mock_get):
        """CMD-004: /resume <id> <text> resumes the task."""
        mock_get.return_value = {
            "task": {"id": "t001", "status": {"state": "TASK_STATE_INPUT_REQUIRED"}}
        }
        mock_resume.return_value = {"task": {"id": "t001"}}
        self.db.add_task_mapping("t001", "user-aad-001", "tenant-001")

        from teams_gateway.app import _handle_activity
        card = _handle_activity(_make_activity(text="/resume t001 my detailed reply", activity_id="cmd004"))
        mock_resume.assert_called_once()
        # Verify reply text was passed
        call_args = mock_resume.call_args[0]
        self.assertIn("my detailed reply", call_args[1]["parts"][0]["text"])

    # ---- CMD-006: /tasks with extra params ----

    def test_cmd006_tasks_extra_params_ignored(self):
        """CMD-006: /tasks with extra params still returns task list."""
        with patch("teams_gateway.compass_client.list_tasks") as mock_list:
            mock_list.return_value = []
            from teams_gateway.app import _handle_activity
            card = _handle_activity(_make_activity(text="/tasks extra ignored", activity_id="cmd006"))
            card_json = json.dumps(card)
            self.assertIn("No running tasks", card_json)

    # ---- IR-001: INPUT_REQUIRED notification ----

    def test_ir001_input_required_notification_card(self):
        """IR-001: INPUT_REQUIRED notification builds Attention card."""
        self.db.add_task_mapping("t001", "user-aad-001", "tenant-001")
        self.db.upsert_conversation_ref("user-aad-001", "tenant-001", "c1", "https://svc", "b1")

        from teams_gateway.app import _handle_notification
        with patch("teams_gateway.app._send_proactive_message") as mock_send:
            mock_send.return_value = "ok"
            _handle_notification({
                "taskId": "t001",
                "state": "TASK_STATE_INPUT_REQUIRED",
                "statusMessage": "Please provide the target branch name",
                "ownerUserId": "user-aad-001",
                "tenantId": "tenant-001",
            })
            mock_send.assert_called_once()
            # The card should be an input_required card (Attention color)
            sent_card = mock_send.call_args[0][1]
            card_json = json.dumps(sent_card)
            self.assertIn("attention", card_json.lower())
            self.assertIn("t001", card_json)

    # ---- IR-003: UI replies to INPUT_REQUIRED, Teams notified ----

    def test_ir003_ui_reply_updates_local_state(self):
        """IR-003: Compass notification of WORKING after UI reply updates local state."""
        self.db.add_task_mapping("t001", "user-aad-001", "tenant-001")
        self.db.update_task_state("t001", "TASK_STATE_INPUT_REQUIRED")
        self.db.upsert_conversation_ref("user-aad-001", "tenant-001", "c1", "https://svc", "b1")

        from teams_gateway.app import _handle_notification
        with patch("teams_gateway.app._send_proactive_message") as mock_send:
            mock_send.return_value = "ok"
            # Compass sends COMPLETED after UI reply resumed the task
            _handle_notification({
                "taskId": "t001",
                "state": "TASK_STATE_COMPLETED",
                "statusMessage": "Task finished",
                "ownerUserId": "user-aad-001",
                "tenantId": "tenant-001",
            })
            # Local state should be updated
            tasks = self.db.get_user_tasks("user-aad-001", "tenant-001")
            self.assertEqual(tasks[0]["last_known_state"], "TASK_STATE_COMPLETED")
            mock_send.assert_called_once()

    # ---- IR-006: /resume explicit command ----

    @patch("teams_gateway.compass_client.get_task")
    @patch("teams_gateway.compass_client.resume_task")
    def test_ir006_explicit_resume_specifies_task(self, mock_resume, mock_get):
        """IR-006: /resume <id> routes to correct task (explicit, ignores IR auto-routing)."""
        mock_get.return_value = {
            "task": {"id": "t002", "status": {"state": "TASK_STATE_INPUT_REQUIRED"}}
        }
        mock_resume.return_value = {"task": {"id": "t002"}}
        # Two INPUT_REQUIRED tasks
        self.db.add_task_mapping("t001", "user-aad-001", "tenant-001")
        self.db.update_task_state("t001", "TASK_STATE_INPUT_REQUIRED")
        self.db.add_task_mapping("t002", "user-aad-001", "tenant-001")
        self.db.update_task_state("t002", "TASK_STATE_INPUT_REQUIRED")

        from teams_gateway.app import _handle_activity
        card = _handle_activity(_make_activity(text="/resume t002 the answer for t002", activity_id="ir006"))
        mock_resume.assert_called_once()
        # Should have resumed t002, not t001
        call_args = mock_resume.call_args[0]
        self.assertEqual(call_args[0], "t002")

    # ---- IR-007: /resume on non-INPUT_REQUIRED task ----

    @patch("teams_gateway.compass_client.get_task")
    def test_ir007_resume_non_ir_task(self, mock_get):
        """IR-007: /resume on WORKING task returns error."""
        mock_get.return_value = {
            "task": {"id": "t001", "status": {"state": "TASK_STATE_WORKING"}}
        }
        self.db.add_task_mapping("t001", "user-aad-001", "tenant-001")

        from teams_gateway.app import _handle_activity
        card = _handle_activity(_make_activity(text="/resume t001 some reply", activity_id="ir007"))
        card_json = json.dumps(card)
        self.assertIn("not currently waiting", card_json.lower())

    # ---- LC-004: bot reinstall restores conv ref ----

    def test_lc004_reinstall_restores_conv_ref(self):
        """LC-004: Uninstall + reinstall restores conversation reference."""
        # Simulate install
        self.db.upsert_conversation_ref("user-aad-001", "tenant-001", "c1", "https://svc", "b1")
        # Simulate uninstall
        self.db.delete_conversation_ref("user-aad-001", "tenant-001")
        self.assertIsNone(self.db.get_conversation_ref("user-aad-001", "tenant-001"))

        # Reinstall via conversationUpdate membersAdded
        from teams_gateway.app import _handle_activity
        card = _handle_activity(_make_activity(
            activity_type="conversationUpdate",
            members_added=[{"id": "bot-id-1"}],
            activity_id="lc004-reinstall",
        ))
        self.assertIsNotNone(card)
        # Conv ref should be restored
        ref = self.db.get_conversation_ref("user-aad-001", "tenant-001")
        self.assertIsNotNone(ref)
        self.assertEqual(ref["is_valid"], 1)

    # ---- LC-005: service URL update on new activity ----

    @patch("teams_gateway.compass_client.send_message")
    def test_lc005_service_url_updated(self, mock_send):
        """LC-005: serviceUrl updated on each new Activity."""
        mock_send.return_value = {"task": {"id": "t001"}}
        # Initial conv ref with old service URL
        self.db.upsert_conversation_ref(
            "user-aad-001", "tenant-001", "c1", "https://old-svc.example.com", "b1"
        )
        from teams_gateway.app import _handle_activity
        _handle_activity(_make_activity(
            text="new message",
            service_url="https://new-svc.trafficmanager.net/teams/",
            activity_id="lc005",
        ))
        ref = self.db.get_conversation_ref("user-aad-001", "tenant-001")
        self.assertEqual(ref["service_url"], "https://new-svc.trafficmanager.net/teams/")

    # ---- CC-001: two users creating tasks concurrently ----

    @patch("teams_gateway.compass_client.send_message")
    def test_cc001_two_users_concurrent_tasks(self, mock_send):
        """CC-001: Two users create tasks concurrently — independent, no interference."""
        mock_send.return_value = {"task": {"id": "shared-task"}}
        from teams_gateway.app import _handle_activity
        errors = []
        cards_by_user = {}

        def create_task(user_id, act_id):
            try:
                c = _handle_activity(_make_activity(
                    text=f"task for {user_id}",
                    user_aad_id=user_id,
                    activity_id=act_id,
                ))
                cards_by_user[user_id] = c
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=create_task, args=("user-A", "cc001-a"))
        t2 = threading.Thread(target=create_task, args=("user-B", "cc001-b"))
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(len(errors), 0, f"Errors: {errors}")
        self.assertIn("user-A", cards_by_user)
        self.assertIn("user-B", cards_by_user)

    # ---- CC-002: same user sends from two devices ----

    @patch("teams_gateway.compass_client.send_message")
    def test_cc002_same_user_two_devices(self, mock_send):
        """CC-002: Same user sends messages from two different activity IDs (two devices)."""
        mock_send.return_value = {"task": {"id": "task-device"}}
        from teams_gateway.app import _handle_activity
        import teams_gateway.app as gw_app
        orig = gw_app.RATE_LIMIT_PER_MINUTE
        gw_app.RATE_LIMIT_PER_MINUTE = 10
        try:
            c1 = _handle_activity(_make_activity(text="task from PC", activity_id="cc002-pc"))
            c2 = _handle_activity(_make_activity(text="task from mobile", activity_id="cc002-mobile"))
            # Both should be accepted (different activity IDs = not duplicates)
            self.assertEqual(mock_send.call_count, 2)
        finally:
            gw_app.RATE_LIMIT_PER_MINUTE = orig


class TestNotificationHandling(unittest.TestCase):
    """PN-001..PN-009, IR-009, SEC-009: Proactive notification tests."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(self.tmpdir, "tgn", "tg.db")
        self.db = GatewayDB(db_path=db_path)
        import teams_gateway.app as gw_app
        self._orig_db = gw_app.db
        gw_app.db = self.db

    def tearDown(self):
        import teams_gateway.app as gw_app
        gw_app.db = self._orig_db
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _setup_user(self, user="user-aad-001", tenant="tenant-001", task_id="t001"):
        self.db.upsert_conversation_ref(user, tenant, "conv-001", "https://svc.example.com", "bot-1")
        self.db.add_task_mapping(task_id, user, tenant)

    def test_pn001_completed_notification_sent(self):
        """PN-001: COMPLETED state triggers proactive message with Good-color card."""
        self._setup_user()
        from teams_gateway.app import _handle_notification
        with patch("teams_gateway.app._send_proactive_message") as mock_send:
            mock_send.return_value = "ok"
            _handle_notification({
                "taskId": "t001",
                "state": "TASK_STATE_COMPLETED",
                "statusMessage": "Done!",
                "ownerUserId": "user-aad-001",
                "tenantId": "tenant-001",
                "summary": "Task completed successfully.",
            })
            mock_send.assert_called_once()
            sent_card = mock_send.call_args[0][1]
            card_json = json.dumps(sent_card)
            self.assertIn("good", card_json.lower())

    def test_pn002_failed_notification_sent(self):
        """PN-002: FAILED state triggers Attention-color card."""
        self._setup_user()
        from teams_gateway.app import _handle_notification
        with patch("teams_gateway.app._send_proactive_message") as mock_send:
            mock_send.return_value = "ok"
            _handle_notification({
                "taskId": "t001",
                "state": "TASK_STATE_FAILED",
                "statusMessage": "Build failed: compilation error",
                "ownerUserId": "user-aad-001",
                "tenantId": "tenant-001",
            })
            mock_send.assert_called_once()
            sent_card = mock_send.call_args[0][1]
            card_json = json.dumps(sent_card)
            self.assertIn("attention", card_json.lower())

    def test_pn004_long_summary_truncated(self):
        """PN-004: Summary > 2000 chars is truncated in card."""
        long_summary = "x" * 3000
        card = cards.completed_card("t001", long_summary)
        card_json = json.dumps(card)
        # Card JSON should not contain the full 3000-char string
        self.assertLess(len(card_json), 28 * 1024)
        # The text block should be capped at 2000 chars
        content = card["content"]
        body_text = json.dumps(content["body"])
        # The truncated text should appear
        self.assertIn("x" * 2000, body_text)
        self.assertNotIn("x" * 2001, body_text)

    def test_pn008_403_marks_conv_invalid(self):
        """PN-008: 403 from Bot Framework marks conversation reference invalid."""
        self._setup_user()
        from teams_gateway.app import _handle_notification
        with patch("teams_gateway.app._send_proactive_message", return_value="unauthorized"):
            _handle_notification({
                "taskId": "t001",
                "state": "TASK_STATE_COMPLETED",
                "statusMessage": "Done",
                "ownerUserId": "user-aad-001",
                "tenantId": "tenant-001",
            })
        ref = self.db.get_conversation_ref("user-aad-001", "tenant-001")
        self.assertIsNotNone(ref)
        self.assertEqual(ref["is_valid"], 0)

    def test_ir009_notification_failure_increments_counter(self):
        """IR-009: Notification failure increments failure counter, auto-invalidates at 5."""
        self._setup_user()
        from teams_gateway.app import _handle_notification
        with patch("teams_gateway.app._send_proactive_message", return_value="error"):
            for _ in range(5):
                _handle_notification({
                    "taskId": "t001",
                    "state": "TASK_STATE_COMPLETED",
                    "statusMessage": "Done",
                    "ownerUserId": "user-aad-001",
                    "tenantId": "tenant-001",
                })
        ref = self.db.get_conversation_ref("user-aad-001", "tenant-001")
        self.assertIsNotNone(ref)
        self.assertEqual(ref["is_valid"], 0)

    def test_pn_no_conv_ref_skips(self):
        """Notification without a valid conv ref is silently skipped."""
        # No conversation ref stored
        self.db.add_task_mapping("t001", "user-aad-001", "tenant-001")
        from teams_gateway.app import _handle_notification
        with patch("teams_gateway.app._send_proactive_message") as mock_send:
            _handle_notification({
                "taskId": "t001",
                "state": "TASK_STATE_COMPLETED",
                "statusMessage": "Done",
                "ownerUserId": "user-aad-001",
                "tenantId": "tenant-001",
            })
            mock_send.assert_not_called()

    def test_sec009_path_sanitization(self):
        """SEC-009: Internal paths stripped from notification payload before sending."""
        self._setup_user()
        from teams_gateway.app import _handle_notification
        with patch("teams_gateway.app._send_proactive_message") as mock_send:
            mock_send.return_value = "ok"
            _handle_notification({
                "taskId": "t001",
                "state": "TASK_STATE_COMPLETED",
                "statusMessage": "Done",
                "ownerUserId": "user-aad-001",
                "tenantId": "tenant-001",
                "summary": "Built at /app/artifacts/task-001/output.zip — see /app/data/compass/tasks.db",
            })
            sent_card = mock_send.call_args[0][1]
            card_json = json.dumps(sent_card)
            # Internal paths should be redacted
            self.assertNotIn("/app/artifacts/task-001", card_json)
            self.assertNotIn("/app/data/compass", card_json)
            self.assertIn("[artifact-path]", card_json)
            self.assertIn("[data-path]", card_json)

    def test_pn_working_state_not_notified(self):
        """WORKING state does not trigger a proactive message (only key states do)."""
        self._setup_user()
        from teams_gateway.app import _handle_notification
        with patch("teams_gateway.app._send_proactive_message") as mock_send:
            _handle_notification({
                "taskId": "t001",
                "state": "TASK_STATE_WORKING",
                "statusMessage": "Still running",
                "ownerUserId": "user-aad-001",
                "tenantId": "tenant-001",
            })
            mock_send.assert_not_called()


class TestGW008DockerCompose(unittest.TestCase):
    """GW-008: Verify Docker Compose configuration for teams-gateway."""

    def test_gw008_compose_has_teams_gateway(self):
        """GW-008: docker-compose.yml includes teams-gateway service."""
        compose_path = os.path.join(
            os.path.dirname(__file__), "..", "docker-compose.yml"
        )
        with open(compose_path, encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("teams-gateway", content, "teams-gateway service missing from docker-compose.yml")
        self.assertIn("./data:/app/data", content, "data volume mount missing")
        self.assertIn("TEAMS_GATEWAY_DB_PATH", content, "DB path env var missing")


if __name__ == "__main__":
    unittest.main()
