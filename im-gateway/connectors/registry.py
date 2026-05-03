"""Connector self-registration registry for the unified IM Gateway."""

from __future__ import annotations

from im_gateway.connectors import IMConnector

_connectors: dict[str, type[IMConnector]] = {}


def register_connector(name: str, cls: type[IMConnector]) -> None:
    """Register a connector class (called at import time by each connector module)."""
    if name in _connectors:
        raise ValueError(f"Connector already registered: {name}")
    _connectors[name] = cls


def init_connectors(config: dict) -> list[IMConnector]:
    """Instantiate all registered connectors whose required config is present."""
    active: list[IMConnector] = []
    for name, cls in _connectors.items():
        if cls.is_configured(config):
            connector = cls(config)
            active.append(connector)
            print(f"[im-gateway] Connector activated: {name}")
        else:
            print(f"[im-gateway] Connector skipped (not configured): {name}")
    return active


def get_connector(name: str) -> type[IMConnector] | None:
    """Look up a registered connector class by name."""
    return _connectors.get(name)


def list_connectors() -> list[str]:
    """Return names of all registered connectors."""
    return list(_connectors.keys())
