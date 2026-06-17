"""Agent definition and base class.

Defines AgentMode (chat / task / single_turn), ExecutionMode (persistent / per-task),
AgentDefinition (declarative agent metadata), and BaseAgent (lifecycle base class).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.checkpoint import CheckpointService
    from framework.event_store import EventStore
    from framework.memory import MemoryService
    from framework.plugin import PluginManager
    from framework.session import SessionService
    from framework.skills import SkillsRegistry
    from framework.task_store import TaskStore


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AgentMode(str, Enum):
    """How the agent interacts with the outside world."""

    CHAT = "chat"                # Full user interaction (Compass)
    TASK = "task"                # Task execution with optional clarification (Team Lead, Dev, Review)
    SINGLE_TURN = "single_turn"  # One-shot, no user interaction (Boundary Agents)


class ExecutionMode(str, Enum):
    """Container lifecycle strategy."""

    PERSISTENT = "persistent"    # Always running (Compass, Team Lead, Boundary)
    PER_TASK = "per-task"        # Launched per task (Dev, Code Review)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AgentDefinition:
    """Declarative description of an agent's identity, mode, and configuration."""

    agent_id: str
    name: str
    description: str
    version: str = "1.0.0"
    mode: AgentMode = AgentMode.TASK
    execution_mode: ExecutionMode = ExecutionMode.PER_TASK
    skills: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    permissions: dict = field(default_factory=dict)
    permission_profile: str = ""  # YAML profile name (e.g. "development", "read_only")
    runtime_backend: str = "connect-agent"
    model: str = "gpt-5-mini"
    runtime_capabilities: dict = field(default_factory=dict)
    workflow: Any = None  # Workflow instance or None
    config: dict = field(default_factory=dict)
    launch_spec: LaunchSpec | None = None              # Container launch specification


@dataclass
class LaunchSpec:
    """Describes how an agent should be launched in a container."""

    cli: str = "claude-code"                    # CLI backend: claude-code | copilot-cli | connect-agent
    image: str = ""                              # Docker image (e.g. "constellation-v2-web-dev:latest")
    mount_docker_socket: bool = False           # Mount host Docker socket for nested containers
    mount_artifact_root: bool = True            # Mount local artifacts/ to /app/artifacts
    env: dict = field(default_factory=dict)     # Static env vars injected into container
    pass_through_env: list[str] = field(default_factory=list)  # Host env vars passed through
    port: int = 0                               # Container port (0 = auto-assign)
    memory: str = ""                             # Memory limit (e.g. "2g", "512m")
    startup_delay_seconds: float = 1.0           # Seconds to wait before health check
    extra_binds: list[str] = field(default_factory=list)  # Additional bind mounts


@dataclass
class AgentServices:
    """Services injected into every agent at construction time."""

    session_service: SessionService
    event_store: EventStore
    memory_service: MemoryService
    skills_registry: SkillsRegistry
    plugin_manager: PluginManager
    checkpoint_service: CheckpointService
    runtime: Any  # AgentRuntimeAdapter
    registry_client: Any  # RegistryClient or None
    task_store: TaskStore = None  # type: ignore[assignment]
    launcher: Any = None  # Launcher or None


# ---------------------------------------------------------------------------
# Base agent
# ---------------------------------------------------------------------------

class BaseAgent:
    """Base class all agents inherit from.

    Subclasses implement ``handle_message`` and ``get_task`` to satisfy the A2A
    protocol.  The ``start`` / ``stop`` hooks manage the compiled workflow and
    optional registry registration.
    """

    def __init__(self, definition: AgentDefinition, services: AgentServices):
        self.definition = definition
        self.services = services

        # Convenience aliases for the most-used services
        self.session_service = services.session_service
        self.event_store = services.event_store
        self.memory_service = services.memory_service
        self.skills_registry = services.skills_registry
        self.plugin_manager = services.plugin_manager
        self.checkpoint_service = services.checkpoint_service
        self.runtime = services.runtime
        self.task_store = services.task_store

        self._compiled_workflow = None
        self._permission_engine: Any = None  # Loaded in start()
        self._registry_instance: dict[str, Any] = {}

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Initialize agent: compile workflow, load permissions, register with Registry."""
        if self.definition.workflow:
            self._compiled_workflow = self.definition.workflow.compile()
        self._load_permission_engine()
        await self._register()

    async def stop(self) -> None:
        """Graceful shutdown hook (override in subclasses if needed)."""
        self._update_registry_instance(status="exited", current_task_id=None)

    # -- A2A interface (to be implemented by subclasses) ---------------------

    async def handle_message(self, message: dict) -> dict:
        """A2A ``POST /message:send`` handler.  Returns a task dict."""
        raise NotImplementedError

    async def get_task(self, task_id: str) -> dict:
        """A2A ``GET /tasks/{id}`` handler."""
        raise NotImplementedError

    async def handle_task_cancel(self, task_id: str, reason: str = "") -> dict:
        """A2A ``POST /tasks/{id}/cancel`` handler.

        Default implementation marks the local task CANCELLED via
        :meth:`TaskStore.cancel_task`. Subclasses that own long-running
        workflow threads (e.g. the office agent) should override this to
        signal the in-flight thread to stop, then call the base behavior.

        Returns a structured response that the A2A server forwards back
        to the caller (e.g. compass forwarding a cancel to office).
        """
        task_store = self.services.task_store if self.services else None
        if task_store is None:
            return {"status": "error", "error": "no task_store"}
        cancelled = task_store.cancel_task(task_id, reason or "cancelled by user")
        return {
            "status": "ok" if cancelled else "already_terminal",
            "task_id": task_id,
            "mode": "local_only",
        }

    async def resume_task(self, task_id: str, resume_value: Any) -> dict:
        """Resume a task that was paused for user input.

        Subclasses with graph workflows should override this to call
        ``CompiledWorkflow.resume()`` with the checkpoint service.
        Default implementation transitions the task back to WORKING and
        re-invokes the workflow from the checkpoint.

        The resumed workflow result is used to build artifacts and completion
        state, not just a hardcoded "Resumed and completed" string.
        """
        from framework.a2a.protocol import Artifact
        from framework.errors import InterruptSignal

        task_store = self.services.task_store
        if task_store is None:
            raise RuntimeError("No task store available")

        task = task_store.get_task(task_id)
        if task is None:
            raise RuntimeError(f"Task {task_id} not found")

        task_store.resume_task(task_id)

        if self._compiled_workflow and self.checkpoint_service:
            config = self._build_run_config(task_id)
            try:
                result = await self._compiled_workflow.resume(config, resume_value)
                # Build artifacts from the resumed workflow result
                summary = ""
                if isinstance(result, dict):
                    summary = (
                        result.get("report_summary")
                        or result.get("implementation_summary")
                        or result.get("summary")
                        or "Resumed and completed"
                    )
                artifacts = [
                    Artifact(
                        name=f"{self.definition.agent_id}-resumed",
                        artifact_type="text/plain",
                        parts=[{"text": summary}],
                        metadata={
                            "agentId": self.definition.agent_id,
                            "taskId": task_id,
                            "resumed": True,
                        },
                    )
                ]
                task_store.complete_task(task_id, artifacts=artifacts, message=summary)
                return task_store.get_task_dict(task_id)
            except InterruptSignal as sig:
                task_store.pause_task(
                    task_id,
                    question=sig.question,
                    interrupt_metadata=sig.metadata,
                )
                return task_store.get_task_dict(task_id)
            except Exception as exc:
                task_store.fail_task(task_id, str(exc))
                return task_store.get_task_dict(task_id)

        return task_store.get_task_dict(task_id)

    # -- Internal ------------------------------------------------------------

    def _load_permission_engine(self) -> None:
        """Load PermissionEngine from the agent's permission_profile and bind
        it to the global ToolRegistry so all tool calls are gated.

        The profile name maps to ``config/permissions/<profile>.yaml``.
        Also accepts a ``permissions`` dict for inline permission sets.
        """
        profile = self.definition.permission_profile
        perms_dict = self.definition.permissions

        if not profile and not perms_dict:
            return

        from framework.permissions import PermissionEngine

        engine: PermissionEngine | None = None

        if profile:
            import os
            from pathlib import Path

            # Look for the YAML file relative to project root
            root = Path(__file__).resolve().parent.parent
            perm_path = root / "config" / "permissions" / f"{profile}.yaml"
            if perm_path.is_file():
                engine = PermissionEngine.from_yaml(str(perm_path))
                print(f"[{self.definition.agent_id}] Permission profile loaded: {profile}")

        if engine is None and perms_dict:
            from framework.permissions import PermissionEngine
            engine = PermissionEngine.from_dict(perms_dict)
            print(f"[{self.definition.agent_id}] Permission engine loaded from definition")

        if engine:
            self._permission_engine = engine
            # NOTE: Do NOT install the engine on the global ToolRegistry here.
            # The engine is passed through RunConfig and installed only during
            # the agent's own workflow execution window (CompiledWorkflow.run()
            # calls get_registry().set_permission_engine(...) before each run
            # and clears it with set_permission_engine(None) on completion).
            # Installing it here permanently would pollute the global registry
            # and block other agents' tools when multiple agents share one
            # process (e.g. in-process tests or the connect-agent runtime).

    def _build_run_config(
        self,
        task_id: str,
        *,
        max_steps: int = 50,
        timeout_seconds: int = 900,
        ephemeral_state: dict | None = None,
    ) -> "Any":
        """Build a standard RunConfig for workflow invocation.

        Centralizes session/checkpoint/event/plugin/permission wiring so
        agent subclasses don't duplicate this boilerplate.
        """
        from framework.workflow import RunConfig

        runtime_state = {
            "_runtime": self.services.runtime,
            "_skills_registry": self.skills_registry,
            "_plugin_manager": self.plugin_manager,
            "_agent_id": self.definition.agent_id,
        }
        if ephemeral_state:
            runtime_state.update(ephemeral_state)

        return RunConfig(
            session_id=task_id,
            thread_id=task_id,
            checkpoint_service=self.checkpoint_service,
            event_store=self.event_store,
            plugin_manager=self.plugin_manager,
            permission_engine=self._permission_engine,
            ephemeral_state=runtime_state,
            max_steps=max_steps,
            timeout_seconds=timeout_seconds,
        )

    async def _register(self) -> None:
        """Register the current live instance with the Capability Registry (best-effort)."""
        client = self.services.registry_client
        if client is None:
            return
        try:
            agent_id = os.environ.get("AGENT_ID", self.definition.agent_id).strip() or self.definition.agent_id
            port = int(os.environ.get("PORT", "0") or 0)
            service_url = os.environ.get("ADVERTISED_BASE_URL", "").strip()

            from framework.config import load_agent_config

            cfg = load_agent_config(agent_id)
            cfg_data = cfg.to_dict()
            effective_port = port or int(cfg_data.get("port", 0) or 0)
            if not service_url and effective_port > 0:
                service_url = f"http://{agent_id}:{effective_port}"

            capabilities = list(cfg_data.get("capabilities") or [])
            if capabilities and service_url:
                payload = {
                    "agentId": cfg_data.get("agent_id", agent_id),
                    "version": str(cfg_data.get("version", self.definition.version or "1.0.0")),
                    "cardUrl": f"{service_url.rstrip('/')}/.well-known/agent-card.json",
                    "capabilities": capabilities,
                    "executionMode": cfg_data.get(
                        "execution_mode",
                        getattr(self.definition.execution_mode, "value", self.definition.execution_mode),
                    ),
                    "displayName": cfg_data.get("name", self.definition.name),
                    "description": cfg_data.get("description", self.definition.description),
                    "registeredBy": "agent-startup",
                }
                scaling_policy = cfg_data.get("scaling_policy") or cfg_data.get("scalingPolicy") or {}
                if scaling_policy:
                    payload["scalingPolicy"] = scaling_policy
                launch_spec = cfg_data.get("launch_spec") or cfg_data.get("launchSpec") or {}
                if launch_spec:
                    payload["launchSpec"] = launch_spec
                client.upsert_agent(payload)

            if not service_url or effective_port <= 0:
                return
            instance = client.register_instance(
                agent_id,
                service_url=service_url,
                port=effective_port,
                container_id=os.environ.get("CONTAINER_ID", "").strip(),
            )
            instance_id = ""
            if isinstance(instance, dict):
                instance_id = str(instance.get("instance_id") or instance.get("instanceId") or "").strip()
            self._registry_instance = {
                "agent_id": agent_id,
                "instance_id": instance_id,
                "service_url": service_url,
                "port": effective_port,
            }
            lifecycle = getattr(self, "_lifecycle", None)
            if lifecycle is not None and hasattr(lifecycle, "configure_registry_updater"):
                lifecycle.configure_registry_updater(self._update_registry_instance)
        except Exception as exc:  # noqa: BLE001
            print(f"[{self.definition.agent_id}] Registry instance registration failed: {exc}")

    def _update_registry_instance(self, **fields: Any) -> None:
        """Best-effort update for the current live instance in Registry."""
        client = self.services.registry_client
        agent_id = str(self._registry_instance.get("agent_id") or "").strip()
        instance_id = str(self._registry_instance.get("instance_id") or "").strip()
        if client is None or not agent_id or not instance_id or not fields:
            return
        try:
            client.update_instance(agent_id, instance_id, **fields)
        except Exception as exc:  # noqa: BLE001
            print(f"[{self.definition.agent_id}] Registry instance update failed: {exc}")

    # -- Memory helpers -------------------------------------------------------

    async def recall_task_context(
        self,
        query: str,
        scope_id: str = "",
        limit: int = 5,
    ) -> str:
        """Search long-term memory for context relevant to *query*.

        Returns a formatted string of the top *limit* matching entries,
        or an empty string if memory is unavailable or nothing matches.

        Call this **before** starting a task workflow to inject prior
        knowledge into the initial state.
        """
        if not self.memory_service:
            return ""
        try:
            entries = await self.memory_service.search(
                query,
                scope="agent",
                scope_id=scope_id or self.definition.agent_id,
                limit=limit,
            )
            if not entries:
                return ""
            lines = [f"- {e.content}" for e in entries]
            return "Relevant past knowledge:\n" + "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            print(f"[{self.definition.agent_id}] Memory recall failed: {exc}")
            return ""

    async def consolidate_task_result(
        self,
        summary: str,
        tags: list[str] | None = None,
        scope_id: str = "",
    ) -> None:
        """Persist *summary* to long-term memory for future task recall.

        Call this **after** a task completes (success or failure) to enable
        future tasks to benefit from the accumulated experience.
        """
        if not self.memory_service or not summary:
            return
        try:
            await self.memory_service.add(
                content=summary,
                scope="agent",
                scope_id=scope_id or self.definition.agent_id,
                tags=tags or [],
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[{self.definition.agent_id}] Memory consolidation failed: {exc}")
