"""LogStore Agent - Aggregates logs from all agents via filesystem subscription.

The package lives at ``agents/log_store/`` (single canonical location).
The legacy ``agents/log-store/`` directory was merged into this one
to avoid two near-identical trees drifting apart.
"""
from agents.log_store.agent import LOGSTORE_DEFINITION, LogStoreAgent
from agents.log_store.api import LogStoreAPI
from agents.log_store.log_aggregator import LOG_LINE_PATTERN, LogAggregator, parse_log_line

__all__ = [
    "LOGSTORE_DEFINITION",
    "LogStoreAgent",
    "LogStoreAPI",
    "LogAggregator",
    "LOG_LINE_PATTERN",
    "parse_log_line",
]
