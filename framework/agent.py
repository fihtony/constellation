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

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Initialize agent: compile workflow, register with Registry."""
        if self.definition.workflow:
            self._compiled_workflow = self.definition.workflow.compile()
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

    # -- Internal ------------------------------------------------------------

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
