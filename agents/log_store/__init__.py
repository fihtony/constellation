"""Import-compatible package wrapper for the log-store agent."""

from agents.log_store.agent import LOGSTORE_DEFINITION, LogStoreAgent

__all__ = ["LogStoreAgent", "LOGSTORE_DEFINITION"]
