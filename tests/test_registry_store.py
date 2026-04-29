from __future__ import annotations

import unittest

from common.registry_store import RegistryStore


class RegistryStoreTests(unittest.TestCase):
    def test_topology_version_advances_on_definition_and_instance_changes(self):
        store = RegistryStore()

        initial = store.topology_state()
        self.assertEqual(initial["version"], 0)

        store.register(
            agent_id="jira-agent",
            version="1.0.0",
            card_url="http://jira:8010/.well-known/agent-card.json",
            capabilities=["jira.ticket.fetch", "jira.comment.add"],
            execution_mode="persistent",
        )
        after_register = store.topology_state()
        self.assertEqual(after_register["version"], 1)

        instance = store.add_instance("jira-agent", "http://jira:8010", 8010, "jira-1")
        after_instance = store.topology_state()
        self.assertEqual(after_instance["version"], 2)

        store.update_instance("jira-agent", instance.instance_id, status="busy", current_task_id="task-1")
        after_update = store.topology_state()
        self.assertEqual(after_update["version"], 3)

        store.heartbeat("jira-agent", instance.instance_id)
        after_heartbeat = store.topology_state()
        self.assertEqual(after_heartbeat["version"], 3)

        store.remove_instance("jira-agent", instance.instance_id)
        after_remove = store.topology_state()
        self.assertEqual(after_remove["version"], 4)

    def test_event_feed_filters_by_version(self):
        store = RegistryStore()

        store.register(
            agent_id="web-agent",
            version="1.0.0",
            card_url="http://web:8050/.well-known/agent-card.json",
            capabilities=["web.task.execute"],
        )
        instance = store.add_instance("web-agent", "http://web:8050", 8050, "web-1")
        store.update_instance("web-agent", instance.instance_id, status="busy", current_task_id="task-9")

        events = store.list_events(since_version=1)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "instance.added")
        self.assertEqual(events[1]["event"], "instance.updated")
        self.assertEqual(events[1]["details"]["current_task_id"], "task-9")


if __name__ == "__main__":
    unittest.main()