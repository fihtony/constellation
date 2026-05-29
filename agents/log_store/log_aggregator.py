"""Wrapper that loads the canonical log-store aggregator module by file path."""
from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


_SOURCE = Path(__file__).resolve().parent.parent / "log-store" / "log_aggregator.py"
_SPEC = spec_from_file_location("agents._log_store_legacy_aggregator", _SOURCE)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load log aggregator module from {_SOURCE}")

_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

LOG_LINE_PATTERN = _MODULE.LOG_LINE_PATTERN
LogAggregator = _MODULE.LogAggregator
parse_log_line = _MODULE.parse_log_line

__all__ = ["LOG_LINE_PATTERN", "LogAggregator", "parse_log_line"]
