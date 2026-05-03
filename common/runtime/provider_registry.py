"""Self-registering runtime provider registry.

Each backend module calls ``register_runtime()`` at import time.
The factory in ``adapter.py`` still owns instantiation caching;
this registry is used for discovery, documentation, and validation.

Usage (in a backend module)::

    from common.runtime.provider_registry import register_runtime
    register_runtime("my-backend", MyAdapter)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from common.runtime.adapter import AgentRuntimeAdapter

_registry: dict[str, type[AgentRuntimeAdapter]] = {}
_launch_registry: dict[str, Callable] = {}


@dataclass
class VolumeMount:
    source: str
    target: str
    read_only: bool = True


@dataclass
class RuntimeLaunchContribution:
    """Host-side preparation required before launching a container for this runtime."""

    mounts: list[VolumeMount] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    launcher_profile: str | None = None
    session_paths: dict[str, str] = field(default_factory=dict)


def register_runtime(name: str, cls: type[AgentRuntimeAdapter]) -> None:
    """Register a runtime provider class under *name*.

    Raises ``ValueError`` if *name* is already registered.
    """
    if name in _registry:
        raise ValueError(f"Runtime provider already registered: {name}")
    _registry[name] = cls


def register_runtime_launch(name: str, fn: Callable) -> None:
    """Register a host-side launch contribution factory for *name*."""
    _launch_registry[name] = fn


def get_runtime_class(name: str) -> type[AgentRuntimeAdapter]:
    """Return the registered adapter class for *name*.

    Raises ``KeyError`` with a helpful message if not found.
    """
    if name not in _registry:
        available = ", ".join(_registry) or "(none)"
        raise KeyError(f"Unknown runtime: '{name}'. Available: {available}")
    return _registry[name]


def get_launch_contribution(name: str, context: dict | None = None) -> RuntimeLaunchContribution | None:
    """Return the launch contribution for *name*, or None if not registered."""
    fn = _launch_registry.get(name)
    if fn is None:
        return None
    return fn(context or {})


def list_runtimes() -> list[str]:
    """Return all registered runtime names."""
    return list(_registry.keys())


def is_registered(name: str) -> bool:
    return name in _registry
