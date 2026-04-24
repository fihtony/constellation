"""Capability Registry in-memory store."""

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
        "deregistered_at",
        "registered_by",
        "deregistered_by",
    )

    def __init__(
        self,
        agent_id,
        version,
        card_url,
        capabilities,
        execution_mode="per-task",
        scaling_policy=None,
        launch_spec=None,
        display_name=None,
        description="",
        registered_by="system",
    ):
        self.agent_id = agent_id
        self.version = version
        self.card_url = card_url
        self.capabilities = capabilities
        self.execution_mode = execution_mode
        self.scaling_policy = scaling_policy or {
            "maxInstances": 5,
            "perInstanceConcurrency": 1,
            "idleTimeoutSeconds": 300,
        }
        self.launch_spec = launch_spec or {}
        self.display_name = display_name or agent_id
        self.description = description
        self.status = "active"
        self.registered_at = time.time()
        self.deregistered_at = None
        self.registered_by = registered_by
        self.deregistered_by = None

    def to_dict(self):
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

    def __init__(self, agent_id, service_url, port, container_id=None):
        self.instance_id = str(uuid.uuid4())[:8]
        self.agent_id = agent_id
        self.container_id = container_id or self.instance_id
        self.service_url = service_url
        self.port = port
        self.status = "idle"
        self.current_task_id = None
        self.last_heartbeat_at = time.time()
        self.idle_since = time.time()

    def to_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


class RegistryStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._definitions = {}
        self._instances = {}

    def register(
        self,
        agent_id,
        version,
        card_url,
        capabilities,
        execution_mode="per-task",
        scaling_policy=None,
        launch_spec=None,
        display_name=None,
        description="",
        registered_by="system",
    ):
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
            return definition

    def deregister(self, agent_id, deregistered_by="system"):
        with self._lock:
            definition = self._definitions.get(agent_id)
            if definition is None:
                return None
            definition.status = "deregistered"
            definition.deregistered_at = time.time()
            definition.deregistered_by = deregistered_by
            return definition

    def get_definition(self, agent_id):
        with self._lock:
            return self._definitions.get(agent_id)

    def list_definitions(self, status_filter=None):
        with self._lock:
            definitions = list(self._definitions.values())
            if status_filter:
                definitions = [definition for definition in definitions if definition.status == status_filter]
            return definitions

    def find_by_capability(self, capability):
        with self._lock:
            return [
                definition
                for definition in self._definitions.values()
                if definition.status == "active" and capability in definition.capabilities
            ]

    def find_any_active(self):
        with self._lock:
            return [definition for definition in self._definitions.values() if definition.status == "active"]

    def add_instance(self, agent_id, service_url, port, container_id=None):
        with self._lock:
            self._instances.setdefault(agent_id, {})
            instance = AgentInstance(agent_id, service_url, port, container_id)
            self._instances[agent_id][instance.instance_id] = instance
            return instance

    def remove_instance(self, agent_id, instance_id):
        with self._lock:
            return self._instances.get(agent_id, {}).pop(instance_id, None)

    def update_instance(self, agent_id, instance_id, **fields):
        with self._lock:
            instance = self._instances.get(agent_id, {}).get(instance_id)
            if instance is None:
                return None
            for key, value in fields.items():
                if hasattr(instance, key):
                    setattr(instance, key, value)
            if fields.get("status") == "idle":
                instance.idle_since = time.time()
            return instance

    def heartbeat(self, agent_id, instance_id):
        return self.update_instance(agent_id, instance_id, last_heartbeat_at=time.time())

    def list_instances(self, agent_id, status_filter=None):
        with self._lock:
            instances = list(self._instances.get(agent_id, {}).values())
            if status_filter:
                instances = [instance for instance in instances if instance.status == status_filter]
            return instances