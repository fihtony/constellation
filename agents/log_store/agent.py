"""Wrapper that loads the canonical log-store agent module by file path."""
from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


_SOURCE = Path(__file__).resolve().parent.parent / "log-store" / "agent.py"
_SPEC = spec_from_file_location("agents._log_store_legacy_agent", _SOURCE)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load log-store agent module from {_SOURCE}")

_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

LOGSTORE_DEFINITION = _MODULE.LOGSTORE_DEFINITION
LogStoreAgent = _MODULE.LogStoreAgent

__all__ = ["LogStoreAgent", "LOGSTORE_DEFINITION"]
