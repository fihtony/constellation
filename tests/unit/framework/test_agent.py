"""Unit tests for framework/agent.py."""
from __future__ import annotations

from unittest.mock import MagicMock

from framework.agent import AgentDefinition, AgentServices, BaseAgent, ExecutionMode


class _DummyAgent(BaseAgent):
    async def handle_message(self, message: dict) -> dict:
        return message

    async def get_task(self, task_id: str) -> dict:
        return {"id": task_id}


def _build_services(registry_client: MagicMock) -> AgentServices:
    return AgentServices(
        session_service=MagicMock(),
        event_store=MagicMock(),
        memory_service=MagicMock(),
        skills_registry=MagicMock(),
        plugin_manager=MagicMock(),
        checkpoint_service=MagicMock(),
        runtime=MagicMock(),
        registry_client=registry_client,
        task_store=MagicMock(),
        launcher=MagicMock(),
    )


async def test_base_agent_start_registers_live_instance(monkeypatch):
    registry_client = MagicMock()
    registry_client.register_instance.return_value = {"instance_id": "inst-123"}
    agent = _DummyAgent(
        AgentDefinition(
            agent_id="team-lead",
            name="Team Lead Agent",
            description="test",
            execution_mode=ExecutionMode.PERSISTENT,
        ),
        _build_services(registry_client),
    )

    monkeypatch.setenv("AGENT_ID", "team-lead")
    monkeypatch.setenv("PORT", "8030")
    monkeypatch.setenv("ADVERTISED_BASE_URL", "http://team-lead:8030")
    monkeypatch.setenv("CONTAINER_ID", "container-123")

    class _FakeConfig:
        def to_dict(self):
            return {
                "agent_id": "team-lead",
                "name": "Team Lead Agent",
                "description": "test",
                "version": "1.0.0",
                "port": 8030,
                "execution_mode": "persistent",
                "capabilities": ["team-lead.task.execute"],
                "scaling_policy": {"maxInstances": 1},
            }

    import framework.config as config_module

    monkeypatch.setattr(config_module, "load_agent_config", lambda *_args, **_kwargs: _FakeConfig())

    await agent.start()

    registry_client.upsert_agent.assert_called_once()
    payload = registry_client.upsert_agent.call_args.args[0]
    assert payload["agentId"] == "team-lead"
    assert payload["cardUrl"] == "http://team-lead:8030/.well-known/agent-card.json"
    assert payload["capabilities"] == ["team-lead.task.execute"]
    assert payload["executionMode"] == "persistent"

    registry_client.register_instance.assert_called_once_with(
        "team-lead",
        service_url="http://team-lead:8030",
        port=8030,
        container_id="container-123",
    )
    assert agent._registry_instance["instance_id"] == "inst-123"


async def test_base_agent_stop_updates_registry_instance_status(monkeypatch):
    registry_client = MagicMock()
    registry_client.register_instance.return_value = {"instance_id": "inst-123"}
    agent = _DummyAgent(
        AgentDefinition(
            agent_id="team-lead",
            name="Team Lead Agent",
            description="test",
            execution_mode=ExecutionMode.PERSISTENT,
        ),
        _build_services(registry_client),
    )

    monkeypatch.setenv("AGENT_ID", "team-lead")
    monkeypatch.setenv("PORT", "8030")
    monkeypatch.setenv("ADVERTISED_BASE_URL", "http://team-lead:8030")

    class _FakeConfig:
        def to_dict(self):
            return {
                "agent_id": "team-lead",
                "name": "Team Lead Agent",
                "description": "test",
                "version": "1.0.0",
                "port": 8030,
                "execution_mode": "persistent",
                "capabilities": ["team-lead.task.execute"],
            }

    import framework.config as config_module

    monkeypatch.setattr(config_module, "load_agent_config", lambda *_args, **_kwargs: _FakeConfig())

    await agent.start()
    await agent.stop()

    registry_client.update_instance.assert_called_with(
        "team-lead",
        "inst-123",
        status="exited",
        current_task_id=None,
    )