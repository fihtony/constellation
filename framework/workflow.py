"""ADK 2.0-style Workflow engine with LangGraph-style checkpoint and interrupt.

A Workflow is defined declaratively via an ``edges`` list.  Each edge is a tuple
describing how control flows between node functions.  Three edge forms are
supported:

*  ``(source, node_fn, target)`` — sequential: run *node_fn*, then go to *target*.
*  ``(source, target)``          — unconditional: go straight from *source* to *target*.
*  ``(source, node_fn, {key: target, ...})`` — conditional: the node returns a
   ``route`` key in its result dict that selects the next node.

Sentinel values ``START`` and ``END`` mark the workflow entry and exit points.

Example::

    wf = Workflow(
        name="demo",
        edges=[
            (START, step_a, step_b),
            (step_b, {
                "yes": step_c,
                "no": step_d,
            }),
            (step_c, END),
            (step_d, step_a),
        ],
    )
    compiled = wf.compile()
    result = await compiled.invoke({"input": "hello"})
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Union

from framework.errors import InterruptSignal, MaxStepsExceeded
from framework.state import StateSchema, merge_state

# ---------------------------------------------------------------------------
# Sentinels
# ---------------------------------------------------------------------------

START = "__START__"
END = "__END__"

# Public re-export of InterruptSignal helper
def interrupt(question: str, **metadata: Any) -> None:
    """Call inside a node function to pause the workflow for human input."""
    raise InterruptSignal(question, metadata)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class RunConfig:
    """Configuration for a single workflow invocation."""

    session_id: str = ""
    thread_id: str = ""
    checkpoint_service: Any = None  # CheckpointService
    event_store: Any = None         # EventStore
    plugin_manager: Any = None      # PluginManager
    permission_engine: Any = None   # PermissionEngine (bound to global ToolRegistry)
    max_steps: int = 100
    timeout_seconds: int = 3600


# ---------------------------------------------------------------------------
# Workflow (declarative definition)
# ---------------------------------------------------------------------------

class Workflow:
    """Declarative workflow builder.

    Parameters
    ----------
    name:
        Human-readable workflow identifier used in logs and checkpoints.
    edges:
        List of edge tuples — see module docstring for formats.
    state_schema:
        Optional type hint for the state dict (documentation only for MVP).
    """

    def __init__(
        self,
        name: str,
        edges: list[tuple],
        state_schema: type | StateSchema | None = None,
    ):
        self.name = name
        self.edges = edges
        # Accept either a type hint (backward compat) or a StateSchema dict.
        self.state_schema = state_schema or dict

    def compile(self) -> CompiledWorkflow:
        """Parse edges into an executable graph representation."""
        nodes, transitions = _build_graph(self.edges)
        return CompiledWorkflow(
            name=self.name,
            nodes=nodes,
            transitions=transitions,
            state_schema=self.state_schema,
        )


# ---------------------------------------------------------------------------
# CompiledWorkflow
# ---------------------------------------------------------------------------

class CompiledWorkflow:
    """Immutable executable graph produced by ``Workflow.compile()``."""

    def __init__(
        self,
        name: str,
        nodes: dict[str, Callable],
        transitions: dict[str, Any],
        state_schema: type,
    ):
        self.name = name
        self.nodes = nodes            # node_name → async callable
        self.transitions = transitions  # node_name → next_name | {route: name}
        self.state_schema = state_schema

    async def invoke(self, state: dict, config: RunConfig | None = None) -> dict:
        """Run the workflow to completion (or until interrupt)."""
        runner = WorkflowRunner(self, config or RunConfig())
        return await runner.run(state)

    async def resume(self, config: RunConfig, resume_value: Any) -> dict:
        """Resume a workflow that was interrupted."""
        runner = WorkflowRunner(self, config)
        return await runner.resume(resume_value)


# ---------------------------------------------------------------------------
# WorkflowRunner — executes one invocation
# ---------------------------------------------------------------------------

class WorkflowRunner:
    """Executes a compiled workflow step by step.

    Supports:
    * checkpoint save/restore for crash recovery
    * interrupt/resume for human-in-the-loop
    * plugin before/after callbacks per node
    * event store auditing
    * max_steps guard against infinite loops
    """

    def __init__(self, workflow: CompiledWorkflow, config: RunConfig):
        self.workflow = workflow
        self.config = config
        self._steps_taken = 0

    async def run(self, state: dict) -> dict:
        """Execute from START until END or interrupt."""
        current_node = START

        # Bind PermissionEngine to the global ToolRegistry so that all tool
        # calls made during this workflow run are permission-checked.
        if self.config.permission_engine:
            from framework.tools.registry import get_registry
            get_registry().set_permission_engine(self.config.permission_engine)

        # Restore from checkpoint if available
        if self.config.checkpoint_service:
            saved = await self.config.checkpoint_service.load(
                self.config.session_id, self.config.thread_id,
            )
            if saved:
                state = saved["state"]
                current_node = saved["next_node"]

        while current_node != END:
            if self._steps_taken >= self.config.max_steps:
                raise MaxStepsExceeded(
                    f"Workflow '{self.workflow.name}' exceeded max_steps={self.config.max_steps}"
                )
            self._steps_taken += 1

            # Resolve the node function
            node_fn = self.workflow.nodes.get(current_node)

            # Fire plugin: before_node
            if self.config.plugin_manager:
                await self.config.plugin_manager.fire("before_node", current_node, state)

            # Execute node
            try:
                result = await _call_node(node_fn, state)
            except InterruptSignal as sig:
                # Persist checkpoint so we can resume later
                if self.config.checkpoint_service:
                    await self.config.checkpoint_service.save(
                        self.config.session_id,
                        self.config.thread_id,
                        {"state": state, "next_node": current_node, "interrupt": sig.question},
                    )
                raise

            # Merge result into state (schema-aware if declared)
            if isinstance(result, dict):
                schema = self.workflow.state_schema if isinstance(self.workflow.state_schema, dict) else None
                merge_state(state, result, schema)

            # Record event
            if self.config.event_store:
                await self.config.event_store.append(
                    session_id=self.config.session_id,
                    event_type="node_completed",
                    content={
                        "node": current_node,
                        "result_keys": list(result.keys()) if isinstance(result, dict) else None,
                    },
                )

            # Fire plugin: after_node (use the name of the just-executed node)
            executed_node = current_node

            # Determine next node via transition table
            current_node = self._resolve_next(current_node, state)

            if self.config.plugin_manager:
                await self.config.plugin_manager.fire("after_node", executed_node, state)

            # Checkpoint after each step
            if self.config.checkpoint_service:
                await self.config.checkpoint_service.save(
                    self.config.session_id,
                    self.config.thread_id,
                    {"state": state, "next_node": current_node},
                )

        # Clean up permission engine binding to avoid global state leaking
        if self.config.permission_engine:
            from framework.tools.registry import get_registry
            get_registry().set_permission_engine(None)

        return state

    async def resume(self, resume_value: Any) -> dict:
        """Resume from interrupt with the provided value."""
        if not self.config.checkpoint_service:
            raise RuntimeError("Cannot resume without checkpoint service")

        saved = await self.config.checkpoint_service.load(
            self.config.session_id, self.config.thread_id,
        )
        if not saved:
            raise RuntimeError("No checkpoint found to resume from")

        state = saved["state"]
        state["_resume_value"] = resume_value
        # Re-enter at the same node that interrupted
        current_node = saved["next_node"]

        # Re-execute the interrupted node (it should check _resume_value)
        node_fn = self.workflow.nodes.get(current_node)
        result = await _call_node(node_fn, state)
        if isinstance(result, dict):
            schema = self.workflow.state_schema if isinstance(self.workflow.state_schema, dict) else None
            merge_state(state, result, schema)

        # Continue from the next transition
        next_node = self._resolve_next(current_node, state)

        # Remove the resume sentinel
        state.pop("_resume_value", None)

        # Checkpoint
        if self.config.checkpoint_service:
            await self.config.checkpoint_service.save(
                self.config.session_id, self.config.thread_id,
                {"state": state, "next_node": next_node},
            )

        # Continue normal execution
        saved_steps = self._steps_taken
        self._steps_taken = 0
        return await self._run_from(next_node, state)

    # -- Internal helpers ---------------------------------------------------

    def _resolve_next(self, current_node: str, state: dict) -> str:
        """Resolve the next node from the transition table."""
        transition = self.workflow.transitions.get(current_node, END)
        if isinstance(transition, dict):
            route_key = state.get("route", "")
            return transition.get(route_key, END)
        return transition

    async def _run_from(self, start_node: str, state: dict) -> dict:
        """Continue execution from a given node."""
        current_node = start_node
        while current_node != END:
            if self._steps_taken >= self.config.max_steps:
                raise MaxStepsExceeded(
                    f"Workflow '{self.workflow.name}' exceeded max_steps={self.config.max_steps}"
                )
            self._steps_taken += 1

            node_fn = self.workflow.nodes.get(current_node)

            if self.config.plugin_manager:
                await self.config.plugin_manager.fire("before_node", current_node, state)

            try:
                result = await _call_node(node_fn, state)
            except InterruptSignal as sig:
                if self.config.checkpoint_service:
                    await self.config.checkpoint_service.save(
                        self.config.session_id, self.config.thread_id,
                        {"state": state, "next_node": current_node, "interrupt": sig.question},
                    )
                raise

            if isinstance(result, dict):
                schema = self.workflow.state_schema if isinstance(self.workflow.state_schema, dict) else None
                merge_state(state, result, schema)

            if self.config.event_store:
                await self.config.event_store.append(
                    session_id=self.config.session_id,
                    event_type="node_completed",
                    content={"node": current_node},
                )

            # Fire plugin: after_node (use the name of the just-executed node)
            executed_node = current_node

            current_node = self._resolve_next(current_node, state)

            if self.config.plugin_manager:
                await self.config.plugin_manager.fire("after_node", executed_node, state)

            if self.config.checkpoint_service:
                await self.config.checkpoint_service.save(
                    self.config.session_id, self.config.thread_id,
                    {"state": state, "next_node": current_node},
                )

        return state


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def _resolve_target(
    target: Any,
    nodes: dict[str, Callable],
) -> str | dict[str, str]:
    """Resolve a target value into a node name, END, or a routing dict."""
    if target is END or target == END:
        return END
    if callable(target):
        name = _node_name(target)
        nodes[name] = target
        return name
    if isinstance(target, dict):
        resolved: dict[str, str] = {}
        for k, v in target.items():
            if callable(v):
                vname = _node_name(v)
                nodes[vname] = v
                resolved[k] = vname
            elif v is END or v == END:
                resolved[k] = END
            else:
                resolved[k] = str(v)
        return resolved
    return str(target)


def _build_graph(
    edges: list[tuple],
) -> tuple[dict[str, Callable], dict[str, Any]]:
    """Parse edge tuples into (nodes, transitions) dicts.

    Edge formats:
      (source, fn, target) — at source, execute fn, then go to target
      (source, target)     — unconditional transition from source to target

    Returns
    -------
    nodes : dict[str, Callable]
        Maps each node name to its async callable.
    transitions : dict[str, str | dict]
        Maps each node name to either a next-node name (unconditional)
        or a dict of {route_key: next_node_name} (conditional).
    """
    nodes: dict[str, Callable] = {}
    transitions: dict[str, Any] = {}

    for edge in edges:
        if len(edge) == 3:
            source, fn, target = edge
            # source_key: where we're coming FROM
            source_key = _node_name(source) if callable(source) else source
            if callable(source):
                nodes[source_key] = source

            # fn must be callable — register it as a node
            fn_key = _node_name(fn)
            nodes[fn_key] = fn

            # Transition: source → fn
            transitions[source_key] = fn_key

            # Transition: fn → target
            transitions[fn_key] = _resolve_target(target, nodes)

        elif len(edge) == 2:
            source, target = edge
            source_key = _node_name(source) if callable(source) else source
            if callable(source):
                nodes[source_key] = source

            transitions[source_key] = _resolve_target(target, nodes)

    return nodes, transitions


def _node_name(fn: Callable) -> str:
    """Derive a stable node name from a callable."""
    return getattr(fn, "__name__", str(fn))


async def _call_node(fn: Callable | None, state: dict) -> dict:
    """Call a node function (sync or async). Returns a dict or empty dict."""
    if fn is None:
        return {}
    if asyncio.iscoroutinefunction(fn):
        result = await fn(state)
    else:
        result = fn(state)
    return result if isinstance(result, dict) else {}
