"""Structured debug logging helpers for development-time agent tracing."""

from __future__ import annotations

import json
import time


def preview_data(value, limit=4000):
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n...[truncated]..."


def debug_log(actor, event, **fields):
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "actor": actor,
        "event": event,
        **fields,
    }
    print(f"[debug] {json.dumps(payload, ensure_ascii=False, default=str)}")