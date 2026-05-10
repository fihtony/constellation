"""Workflow state type definitions.

For MVP the state is a plain dict.  This module defines type aliases and
helpers for working with state.
"""
from __future__ import annotations

from typing import Any

# The canonical workflow state type — a plain dict for MVP.
WorkflowState = dict[str, Any]


def merge_state(base: WorkflowState, delta: WorkflowState) -> WorkflowState:
    """Shallow-merge *delta* into *base* and return the updated dict.

    This is a simple ``dict.update`` for MVP; a future version may use
    LangGraph-style Channel + Reducer semantics for concurrent writes.
    """
    base.update(delta)
    return base
