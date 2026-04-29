"""Root pytest conftest.py.

Makes hyphen-named agent directories importable under their underscore aliases.
E.g. `teams-gateway/` is importable as `teams_gateway`.
"""
from __future__ import annotations

import os
import sys
import types

_ROOT = os.path.dirname(__file__)

# Ensure project root is on sys.path so `common`, `registry`, etc. resolve.
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _register_hyphen_package(dir_name: str, module_name: str) -> None:
    """Register a hyphen-named directory as an importable Python package."""
    pkg_path = os.path.join(_ROOT, dir_name)
    if not os.path.isdir(pkg_path):
        return
    if module_name in sys.modules:
        return
    pkg = types.ModuleType(module_name)
    pkg.__path__ = [pkg_path]  # type: ignore[assignment]
    pkg.__package__ = module_name
    pkg.__spec__ = None  # type: ignore[assignment]
    sys.modules[module_name] = pkg


_register_hyphen_package("teams-gateway", "teams_gateway")
