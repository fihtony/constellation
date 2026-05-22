"""In-memory capability registry store for Constellation v2."""

from __future__ import annotations

import threading
import time
import uuid


class AgentDefinition:
    __slots__ = (
        "agent_id",
        "version",
        "card_url",
        "capabilities",
        "execution_mode",
        "scaling_policy",
        "launch_spec",
        "display_name",
        "description",
        "status",
        "registered_at",
        "registered_by",
    )

    def __init__(
        self,
        *,
        agent_id: str,
        version: str,
        card_url: str,
        capabilities: list[str],
        execution_mode: str = "per-task",
        scaling_policy: dict | None = None,
        launch_spec: dict | None = None,
        display_name: str | None = None,
        description: str = "",
        registered_by: str = "system",
    ) -> None:
        self.agent_id = agent_id
        self.version = version
        self.card_url = card_url
        self.capabilities = list(capabilities or [])
        self.execution_mode = execution_mode
        self.scaling_policy = scaling_policy or {
            "maxInstances": 5,
            "perInstanceConcurrency": 1,
            "idleTimeoutSeconds": 300,
        }
        self.launch_spec = dict(launch_spec or {})
        self.display_name = display_name or agent_id
        self.description = description
        self.status = "active"
        self.registered_at = time.time()
        self.registered_by = registered_by

    def to_dict(self) -> dict:
        return {slot: getattr(self, slot) for slot in self.__slots__}


class AgentInstance:
    __slots__ = (
        "instance_id",
        "agent_id",
        "container_id",
        "service_url",
        "port",
        "status",
        "current_task_id",
        "last_heartbeat_at",
        "idle_since",
    )

    def __init__(self, *, agent_id: str, service_url: str, port: int, container_id: str = "") -> None:
        self.instance_id = str(uuid.uuid4())[:8]
        self.agent_id = agent_id
        self.container_id = container_id or self.instance_id
        self.service_url = service_url
        self.port = int(port or 0)
        self.status = "idle"
        self.current_task_id = None
        self.last_heartbeat_at = time.time()
        self.idle_since = time.time()

    def to_dict(self) -> dict:
        return {slot: getattr(self, slot) for slot in self.__slots__}


class RegistryStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._definitions: dict[str, AgentDefinition] = {}
        self._instances: dict[str, dict[str, AgentInstance]] = {}
        self._topology_version = 0
        self._topology_updated_at = time.time()
        self._events: list[dict] = []

    def _record_event(self, event_type: str, *, agent_id: str = "", instance_id: str = "", details: dict | None = None) -> None:
        self._topology_version += 1
        self._topology_updated_at = time.time()
        event = {
            "version": self._topology_version,
            "ts": self._topology_updated_at,
            "event": event_type,
        }
        if agent_id:
            event["agentId"] = agent_id
        if instance_id:
            event["instanceId"] = instance_id
        if details:
            event["details"] = details
        self._events.append(event)
        if len(self._events) > 200:
            self._events = self._events[-200:]

    def topology_state(self) -> dict:
        with self._lock:
            return {
                "version": self._topology_version,
                "updatedAt": self._topology_updated_at,
            }

    def list_events(self, since_version: int = 0) -> list[dict]:
        with self._lock:
            return [event.copy() for event in self._events if event["version"] > since_version]

    def register(
        self,
        *,
        agent_id: str,
        version: str,
        card_url: str,
        capabilities: list[str],
        execution_mode: str = "per-task",
        scaling_policy: dict | None = None,
        launch_spec: dict | None = None,
        display_name: str | None = None,
        description: str = "",
        registered_by: str = "system",
    ) -> AgentDefinition:
        with self._lock:
            definition = AgentDefinition(
                agent_id=agent_id,
                version=version,
                card_url=card_url,
                capabilities=capabilities,
                execution_mode=execution_mode,
                scaling_policy=scaling_policy,
                launch_spec=launch_spec,
                display_name=display_name,
                description=description,
                registered_by=registered_by,
            )
            self._definitions[agent_id] = definition
            self._instances.setdefault(agent_id, {})
            self._record_event(
                "agent.registered",
                agent_id=agent_id,
                details={
                    "capabilities": list(definition.capabilities),
                    "executionMode": definition.execution_mode,
                },
            )
            return definition

    def get_definition(self, agent_id: str) -> AgentDefinition | None:
        with self._lock:
            return self._definitions.get(agent_id)

    def list_definitions(self, *, active_only: bool = False) -> list[AgentDefinition]:
        with self._lock:
            values = list(self._definitions.values())
            if active_only:
                values = [definition for definition in values if definition.status == "active"]
            return values

    def find_by_capability(self, capability: str) -> list[AgentDefinition]:
        with self._lock:
            return [
                definition
                for definition in self._definitions.values()
                if definition.status == "active" and capability in definition.capabilities
            ]

    def add_instance(self, *, agent_id: str, service_url: str, port: int, container_id: str = "") -> AgentInstance:
        with self._lock:
            self._instances.setdefault(agent_id, {})
            for instance in self._instances[agent_id].values():
                if container_id and instance.container_id == container_id:
                    instance.service_url = service_url
                    instance.port = int(port or 0)
                    instance.last_heartbeat_at = time.time()
                    self._record_event(
                        "instance.updated",
                        agent_id=agent_id,
                        instance_id=instance.instance_id,
                        details={"serviceUrl": service_url, "port": instance.port},
                    )
                    return instance

            instance = AgentInstance(
                agent_id=agent_id,
                service_url=service_url,
                port=port,
                container_id=container_id,
            )
            self._instances[agent_id][instance.instance_id] = instance
            self._record_event(
                "instance.added",
                agent_id=agent_id,
                instance_id=instance.instance_id,
                details={"serviceUrl": service_url, "port": instance.port},
            )
            return instance

    def list_instances(self, agent_id: str, *, active_only: bool = False) -> list[AgentInstance]:
        with self._lock:
            values = list(self._instances.get(agent_id, {}).values())
            if active_only:
                values = [instance for instance in values if instance.status in {"idle", "busy"}]
            return values

    def update_instance(self, agent_id: str, instance_id: str, **fields) -> AgentInstance | None:
        with self._lock:
            instance = self._instances.get(agent_id, {}).get(instance_id)
            if instance is None:
                return None
            changed: dict[str, object] = {}
            for key, value in fields.items():
                if hasattr(instance, key) and getattr(instance, key) != value:
                    setattr(instance, key, value)
                    changed[key] = value
            if fields.get("status") == "idle":
                instance.idle_since = time.time()
                changed["idle_since"] = instance.idle_since
            if changed:
                self._record_event(
                    "instance.updated",
                    agent_id=agent_id,
                    instance_id=instance_id,
                    details=changed,
                )
            return instance

    def heartbeat(self, agent_id: str, instance_id: str) -> AgentInstance | None:
        return self.update_instance(agent_id, instance_id, last_heartbeat_at=time.time())


store = RegistryStore()
