"""Agent definition and base class.

Defines AgentMode (chat / task / single_turn), ExecutionMode (persistent / per-task),
AgentDefinition (declarative agent metadata), and BaseAgent (lifecycle base class).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
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
    workflow: Any = None  # Workflow instance or None
    config: dict = field(default_factory=dict)


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

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Initialize agent: compile workflow, load permissions, register with Registry."""
        if self.definition.workflow:
            self._compiled_workflow = self.definition.workflow.compile()
        self._load_permission_engine()
        await self._register()

    async def stop(self) -> None:
        """Graceful shutdown hook (override in subclasses if needed)."""

    # -- A2A interface (to be implemented by subclasses) ---------------------

    async def handle_message(self, message: dict) -> dict:
        """A2A ``POST /message:send`` handler.  Returns a task dict."""
        raise NotImplementedError

    async def get_task(self, task_id: str) -> dict:
        """A2A ``GET /tasks/{id}`` handler."""
        raise NotImplementedError

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
            from framework.tools.registry import get_registry
            get_registry().set_permission_engine(engine)

    def _build_run_config(
        self,
        task_id: str,
        *,
        max_steps: int = 50,
        timeout_seconds: int = 900,
    ) -> "Any":
        """Build a standard RunConfig for workflow invocation.

        Centralizes session/checkpoint/event/plugin/permission wiring so
        agent subclasses don't duplicate this boilerplate.
        """
        from framework.workflow import RunConfig

        return RunConfig(
            session_id=task_id,
            thread_id=task_id,
            checkpoint_service=self.checkpoint_service,
            event_store=self.event_store,
            plugin_manager=self.plugin_manager,
            permission_engine=self._permission_engine,
            max_steps=max_steps,
            timeout_seconds=timeout_seconds,
        )

    async def _register(self) -> None:
        """Register with the Capability Registry (best-effort)."""
        client = self.services.registry_client
        if client is None:
            return
        try:
            await client.register_agent(self.definition.agent_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[{self.definition.agent_id}] Registry registration failed: {exc}")

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
