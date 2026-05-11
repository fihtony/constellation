"""Workflow state type definitions.

Supports two levels of state merging:

1. **Simple overwrite** (default / no schema): ``dict.update`` — last write wins.
2. **Channel + Reducer** (LangGraph-style): declare a ``StateSchema`` dict that maps
   state keys to ``Channel`` instances.  Each channel optionally carries a
   *reducer* function that is called as ``reducer(existing, new_value)`` to
   produce the merged value.  This enables concurrent node writes to the same
   key — e.g. append-only log lists, shallow-merged metadata dicts.

Built-in reducers
-----------------
- ``append_reducer``  — list concatenation / item append
- ``merge_reducer``   — shallow dict merge (last-write-wins per sub-key)

Usage
-----
::

    from framework.state import Channel, StateSchema, append_reducer

    # Declare schema for a workflow that has a running log
    my_schema: StateSchema = {
        "events": Channel(reducer=append_reducer, default=[]),
        "metadata": Channel(reducer=merge_reducer, default={}),
    }

    wf = Workflow(name="demo", edges=[...], state_schema=my_schema)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# The canonical workflow state type — a plain dict.
WorkflowState = dict[str, Any]


# ---------------------------------------------------------------------------
# Channel descriptor
# ---------------------------------------------------------------------------

@dataclass
class Channel:
    """Describes how a state key is merged when a workflow node writes to it.

    Attributes
    ----------
    reducer:
        ``callable(existing_value, new_value) -> merged_value``.
        ``None`` means *last-write-wins* (overwrite channel).
    default:
        The seed value used when the key is absent from the state dict.
    """

    reducer: Callable[[Any, Any], Any] | None = None
    default: Any = None


# Type alias: ``{state_key: Channel}``
StateSchema = dict[str, Channel]


# ---------------------------------------------------------------------------
# Built-in reducers
# ---------------------------------------------------------------------------

def append_reducer(existing: list | None, new: Any) -> list:
    """Append *new* items to *existing* list.

    * ``existing + new``          when *new* is a list.
    * ``existing + [new]``        when *new* is a single item.
    * Returns ``[new]`` (or ``new`` as list) when *existing* is ``None``.
    """
    if existing is None:
        existing = []
    if isinstance(new, list):
        return existing + new
    return existing + [new]


def merge_reducer(existing: dict | None, new: dict | None) -> dict:
    """Shallow-merge *new* into *existing* dict (last-write-wins per sub-key)."""
    if existing is None:
        existing = {}
    result = dict(existing)
    result.update(new or {})
    return result


# ---------------------------------------------------------------------------
# State merge helper
# ---------------------------------------------------------------------------

def merge_state(
    base: WorkflowState,
    delta: WorkflowState,
    schema: StateSchema | None = None,
) -> WorkflowState:
    """Merge *delta* into *base*, respecting channel reducers when *schema* is given.

    Merge rules
    -----------
    * **No schema** (default): ``base.update(delta)`` — overwrite semantics.
    * **Schema present**:

      - Key with ``Channel(reducer=None)`` or key absent from schema → overwrite.
      - Key with a *reducer* → ``reducer(existing, new_value)`` → stored result.
      - Missing key in *base* uses ``channel.default`` as the existing seed.
    """
    if not schema:
        base.update(delta)
        return base

    for key, value in delta.items():
        channel = schema.get(key)
        if channel is None or channel.reducer is None:
            # Overwrite channel (default behaviour)
            base[key] = value
        else:
            existing = base.get(key, channel.default)
            base[key] = channel.reducer(existing, value)

    return base
