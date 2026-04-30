#!/usr/bin/env python3
"""Tests for infrastructure modules: startup_backoff, install_slug, command_gate, circuit_breaker."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# startup_backoff
# ---------------------------------------------------------------------------

class StartupBackoffTests(unittest.TestCase):
    def _tmp_state(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(path)
        return path

    def test_first_start_no_delay(self):
        from common.startup_backoff import enforce_startup_backoff
        path = self._tmp_state()
        delay = enforce_startup_backoff(state_file=path)
        self.assertEqual(delay, 0)

    def test_second_start_no_delay(self):
        from common.startup_backoff import enforce_startup_backoff
        path = self._tmp_state()
        enforce_startup_backoff(state_file=path)
        delay = enforce_startup_backoff(state_file=path)
        self.assertEqual(delay, 0)

    def test_third_start_has_delay(self):
        from common.startup_backoff import BACKOFF_SCHEDULE, enforce_startup_backoff
        path = self._tmp_state()
        with unittest.mock.patch("common.startup_backoff.time.sleep") as mock_sleep:
            enforce_startup_backoff(state_file=path)
            enforce_startup_backoff(state_file=path)
            delay = enforce_startup_backoff(state_file=path)
        self.assertEqual(delay, BACKOFF_SCHEDULE[2])
        mock_sleep.assert_called_once_with(BACKOFF_SCHEDULE[2])

    def test_reset_clears_state(self):
        from common.startup_backoff import current_attempt, enforce_startup_backoff, reset_startup_backoff
        path = self._tmp_state()
        enforce_startup_backoff(state_file=path)
        enforce_startup_backoff(state_file=path)
        self.assertEqual(current_attempt(state_file=path), 2)
        reset_startup_backoff(state_file=path)
        self.assertEqual(current_attempt(state_file=path), 0)

    def test_reset_when_no_file_is_noop(self):
        from common.startup_backoff import reset_startup_backoff
        reset_startup_backoff(state_file="/tmp/no-such-backoff-file.json")

    def test_max_backoff_capped(self):
        from common.startup_backoff import BACKOFF_SCHEDULE, enforce_startup_backoff
        path = self._tmp_state()
        # Simulate many crashes beyond the schedule length
        with unittest.mock.patch("common.startup_backoff.time.sleep"):
            for _ in range(20):
                delay = enforce_startup_backoff(state_file=path)
        self.assertEqual(delay, BACKOFF_SCHEDULE[-1])


import unittest.mock


# ---------------------------------------------------------------------------
# install_slug
# ---------------------------------------------------------------------------

class InstallSlugTests(unittest.TestCase):
    def test_returns_8_hex_chars(self):
        from common.install_slug import get_install_slug
        slug = get_install_slug()
        self.assertEqual(len(slug), 8)
        int(slug, 16)  # should not raise

    def test_deterministic(self):
        from common.install_slug import get_install_slug
        self.assertEqual(get_install_slug("/tmp/my-project"), get_install_slug("/tmp/my-project"))

    def test_different_paths_different_slugs(self):
        from common.install_slug import get_install_slug
        self.assertNotEqual(get_install_slug("/tmp/proj-a"), get_install_slug("/tmp/proj-b"))

    def test_default_root_is_repo_root(self):
        from common.install_slug import get_install_slug
        slug = get_install_slug()
        # Must be 8 chars, and the same on repeated calls
        self.assertEqual(slug, get_install_slug())


# ---------------------------------------------------------------------------
# command_gate
# ---------------------------------------------------------------------------

class CommandGateTests(unittest.TestCase):
    def _gate(self, text, *, role="user"):
        from common.command_gate import gate_message
        return gate_message(text, role=role)

    def test_normal_message_passes(self):
        from common.command_gate import GateResult
        self.assertEqual(self._gate("implement feature X"), GateResult.PASS)

    def test_empty_message_passes(self):
        from common.command_gate import GateResult
        self.assertEqual(self._gate(""), GateResult.PASS)

    def test_filtered_command_filtered(self):
        from common.command_gate import GateResult
        self.assertEqual(self._gate("/help"), GateResult.FILTER)
        self.assertEqual(self._gate("/login with args"), GateResult.FILTER)
        self.assertEqual(self._gate("/doctor"), GateResult.FILTER)

    def test_admin_command_denied_for_user(self):
        from common.command_gate import GateResult
        self.assertEqual(self._gate("/clear", role="user"), GateResult.DENY)
        self.assertEqual(self._gate("/reset args", role="user"), GateResult.DENY)

    def test_admin_command_allowed_for_admin(self):
        from common.command_gate import GateResult
        self.assertEqual(self._gate("/clear", role="admin"), GateResult.PASS)
        self.assertEqual(self._gate("/debug", role="owner"), GateResult.PASS)
        self.assertEqual(self._gate("/compact", role="tech-lead"), GateResult.PASS)

    def test_unknown_slash_command_passes(self):
        from common.command_gate import GateResult
        # Unknown slash commands are passed through so agents can handle them
        self.assertEqual(self._gate("/custom-skill args"), GateResult.PASS)

    def test_case_insensitive_command_matching(self):
        from common.command_gate import GateResult
        self.assertEqual(self._gate("/HELP"), GateResult.FILTER)
        self.assertEqual(self._gate("/Help"), GateResult.FILTER)


# ---------------------------------------------------------------------------
# circuit_breaker
# ---------------------------------------------------------------------------

class CircuitBreakerTests(unittest.TestCase):
    def _make(self, threshold=3, reset=60):
        from common.circuit_breaker import CircuitBreaker
        return CircuitBreaker(name="test", failure_threshold=threshold, reset_timeout=reset)

    def test_closed_by_default(self):
        cb = self._make()
        self.assertEqual(cb.state, "closed")

    def test_successful_calls_pass_through(self):
        cb = self._make()
        result = cb.call(lambda: "ok")
        self.assertEqual(result, "ok")

    def test_opens_after_threshold_failures(self):
        from common.circuit_breaker import CircuitOpenError
        cb = self._make(threshold=3)
        for _ in range(3):
            try:
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
            except RuntimeError:
                pass
        self.assertEqual(cb.state, "open")
        with self.assertRaises(CircuitOpenError):
            cb.call(lambda: "blocked")

    def test_half_open_after_reset_timeout(self):
        cb = self._make(threshold=1, reset=0.05)
        try:
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        except RuntimeError:
            pass
        self.assertEqual(cb.state, "open")
        time.sleep(0.1)
        self.assertEqual(cb.state, "half-open")

    def test_recovery_from_half_open(self):
        cb = self._make(threshold=1, reset=0.05)
        try:
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        except RuntimeError:
            pass
        time.sleep(0.1)
        result = cb.call(lambda: "recovered")
        self.assertEqual(result, "recovered")
        self.assertEqual(cb.state, "closed")

    def test_manual_reset(self):
        from common.circuit_breaker import CircuitOpenError
        cb = self._make(threshold=1)
        try:
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        except RuntimeError:
            pass
        self.assertEqual(cb.state, "open")
        cb.reset()
        self.assertEqual(cb.state, "closed")
        result = cb.call(lambda: "ok after reset")
        self.assertEqual(result, "ok after reset")

    def test_thread_safe(self):
        """Multiple threads can call simultaneously without data corruption."""
        from common.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker(name="threaded", failure_threshold=100, reset_timeout=60)
        errors = []

        def _work():
            try:
                cb.call(lambda: "ok")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_work) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(cb.state, "closed")

    def test_repr(self):
        cb = self._make()
        r = repr(cb)
        self.assertIn("test", r)
        self.assertIn("closed", r)


# ---------------------------------------------------------------------------
# agent_bus
# ---------------------------------------------------------------------------

class AgentBusTests(unittest.TestCase):
    def test_invalid_destination_raises(self):
        from common.agent_bus import AgentBus
        bus = AgentBus()
        with self.assertRaises(ValueError):
            bus.send("no-prefix-here", "content")

    def test_unknown_prefix_raises(self):
        from common.agent_bus import AgentBus
        bus = AgentBus()
        with self.assertRaises(ValueError):
            bus.send("ftp:some-address", "content")

    def test_callback_send(self):
        from common.agent_bus import AgentBus
        from unittest.mock import patch, Mock
        import json

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"ok": true}'

        bus = AgentBus()
        with patch("common.agent_bus.urlopen", return_value=_Resp()):
            result = bus.send("callback:http://compass:8080/tasks/t1/callbacks", "done")
        self.assertEqual(result, {"ok": True})

    def test_im_send_returns_logged(self):
        from common.agent_bus import AgentBus
        bus = AgentBus()
        result = bus.send("im:slack-channel-123", "hello")
        self.assertEqual(result["status"], "logged")
        self.assertEqual(result["channel"], "slack-channel-123")

    def test_agent_send_when_no_registry(self):
        from common.agent_bus import AgentBus
        from urllib.error import URLError
        bus = AgentBus(registry_url="http://nonexistent-host:9999")
        with self.assertRaises(URLError):
            bus.send("agent:scm.pr.create", "{}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
