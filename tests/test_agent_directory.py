from __future__ import annotations

import threading
import unittest

from common.agent_directory import AgentDirectory, CapabilityUnavailableError


class _FakeRegistryClient:
    def __init__(self):
        self.agents = []
        self.version = 0
        self.find_any_active_calls = 0

    def find_any_active(self):
        self.find_any_active_calls += 1
        return self.agents

    def get_topology(self):
        return {"version": self.version, "updatedAt": 123.0 + self.version}

    def get_events(self, since_version=0):
        return {"version": self.version, "updatedAt": 123.0 + self.version, "events": []}


class AgentDirectoryTests(unittest.TestCase):
    def test_find_capability_refreshes_cache_on_miss(self):
        registry = _FakeRegistryClient()
        directory = AgentDirectory("team-lead-agent", registry, cache_ttl_seconds=999, watch_interval_seconds=999)

        registry.agents = []
        registry.version = 1
        self.assertEqual(directory.find_capability("jira.ticket.fetch"), [])

        registry.agents = [
            {
                "agent_id": "jira-agent",
                "capabilities": ["jira.ticket.fetch"],
                "instances": [{"instance_id": "jira-1", "status": "idle", "service_url": "http://jira:8010"}],
            }
        ]
        registry.version = 2

        match = directory.find_capability("jira.ticket.fetch")

        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["agent_id"], "jira-agent")

    def test_resolve_capability_prefers_idle_instance(self):
        registry = _FakeRegistryClient()
        registry.agents = [
            {
                "agent_id": "ui-design-agent",
                "capabilities": ["figma.page.fetch"],
                "instances": [
                    {"instance_id": "ui-1", "status": "busy", "service_url": "http://ui-design:8040"},
                    {"instance_id": "ui-2", "status": "idle", "service_url": "http://ui-design:8041"},
                ],
            }
        ]
        registry.version = 1
        directory = AgentDirectory("team-lead-agent", registry, cache_ttl_seconds=999, watch_interval_seconds=999)

        agent, instance = directory.resolve_capability("figma.page.fetch")

        self.assertEqual(agent["agent_id"], "ui-design-agent")
        self.assertEqual(instance["instance_id"], "ui-2")

    def test_resolve_capability_raises_when_capability_missing(self):
        registry = _FakeRegistryClient()
        directory = AgentDirectory("web-agent", registry, cache_ttl_seconds=999, watch_interval_seconds=999)

        with self.assertRaises(CapabilityUnavailableError):
            directory.resolve_capability("scm.pr.create")

    def test_find_capability_uses_fresh_cache_without_deadlock(self):
        registry = _FakeRegistryClient()
        registry.agents = [
            {
                "agent_id": "jira-agent",
                "capabilities": ["jira.ticket.fetch"],
                "instances": [{"instance_id": "jira-1", "status": "idle", "service_url": "http://jira:8010"}],
            }
        ]
        registry.version = 1
        directory = AgentDirectory("web-agent", registry, cache_ttl_seconds=999, watch_interval_seconds=999)
        directory.refresh(force=True)

        baseline_calls = registry.find_any_active_calls
        result: list[list[dict]] = []

        worker = threading.Thread(
            target=lambda: result.append(directory.find_capability("jira.ticket.fetch")),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive(), "find_capability should not deadlock on a fresh cache")
        self.assertEqual(len(result[0]), 1)
        self.assertEqual(registry.find_any_active_calls, baseline_calls)


if __name__ == "__main__":
    unittest.main()