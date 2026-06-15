"""Robust JSON extraction for LLM responses.

LLM outputs from different backends (claude-code, copilot-cli, codex-cli,
connect-agent) and different models share a small set of formatting habits
that break naive ``re.search(r"\\{.*\\}")`` parsing:

* Some models wrap their reasoning in ``<think>...</think>`` blocks before
  emitting the structured output.
* Many models still emit ``` ```json ... ``` ``` fences even when the
  system prompt forbids them.
* When a prompt asks for one JSON object but the model is "explaining"
  itself, the response can contain *multiple* JSON-looking fragments.
* Nested objects with ``{`` characters inside string values break greedy
  regex matching.

This module provides one shared utility with deterministic, balanced-brace
scanning so every agent and adapter gets the same parser. It is intentionally
free of any agent-specific schema knowledge — callers pass ``required_keys``
when they need to pick a particular candidate.
"""
from __future__ import annotations

import json
import re
from typing import Iterable

__all__ = [
    "extract_json_object",
    "extract_json_array",
    "extract_first_json",
    "strip_think_blocks",
    "strip_code_fences",
]


_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.IGNORECASE | re.DOTALL)
_LEADING_FENCE_RE = re.compile(r"^\s*```(?:[a-zA-Z0-9_+-]+)?\s*\n?")
_TRAILING_FENCE_RE = re.compile(r"\n?\s*```\s*$")


def strip_think_blocks(text: str) -> str:
    """Remove every ``<think>...</think>`` block from *text* (multi-line safe)."""
    if not text:
        return ""
    return _THINK_BLOCK_RE.sub("", text)


def strip_code_fences(text: str) -> str:
    """Strip a single surrounding ``` fence if present.

    Only strips the outermost fence pair — leaves inner fenced sub-blocks
    alone so we don't corrupt embedded examples.
    """
    if not text:
        return ""
    cleaned = _LEADING_FENCE_RE.sub("", text, count=1)
    cleaned = _TRAILING_FENCE_RE.sub("", cleaned, count=1)
    return cleaned


def _sanitize(text: str) -> str:
    """Apply the standard hygiene chain: trim → strip <think> → strip fences."""
    if not text:
        return ""
    cleaned = text.strip()
    cleaned = strip_think_blocks(cleaned).strip()
    cleaned = strip_code_fences(cleaned).strip()
    return cleaned


def _iter_balanced_spans(text: str, open_char: str, close_char: str) -> Iterable[tuple[int, int]]:
    """Yield ``(start, end_exclusive)`` index pairs for every balanced span.

    A balanced span is a substring that begins with *open_char*, contains
    matching nested *open_char*/*close_char* pairs, and is closed by the
    corresponding *close_char*. Inside a JSON string (a double-quoted run,
    handling backslash escapes) brackets are ignored.

    The scanner does not require valid JSON between the brackets — that is
    the caller's responsibility (``json.loads``). It only guarantees that
    the bracket nesting is balanced.
    """
    if not text:
        return
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == open_char:
            if depth == 0:
                start = i
            depth += 1
            continue
        if ch == close_char and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                yield start, i + 1
                start = -1


def _candidate_dicts(text: str) -> list[dict]:
    """Return every parseable JSON-object candidate found in *text*."""
    candidates: list[dict] = []
    seen_spans: set[tuple[int, int]] = set()
    for start, end in _iter_balanced_spans(text, "{", "}"):
        if (start, end) in seen_spans:
            continue
        seen_spans.add((start, end))
        snippet = text[start:end]
        try:
            loaded = json.loads(snippet)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(loaded, dict):
            candidates.append(loaded)
    return candidates


def _candidate_lists(text: str) -> list[list]:
    """Return every parseable JSON-array candidate found in *text*."""
    candidates: list[list] = []
    seen_spans: set[tuple[int, int]] = set()
    for start, end in _iter_balanced_spans(text, "[", "]"):
        if (start, end) in seen_spans:
            continue
        seen_spans.add((start, end))
        snippet = text[start:end]
        try:
            loaded = json.loads(snippet)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(loaded, list):
            candidates.append(loaded)
    return candidates


def extract_json_object(
    text: str,
    *,
    required_keys: set[str] | None = None,
) -> dict | None:
    """Best-effort extraction of a JSON object from a mixed-content LLM response.

    The function:
      1. Strips ``<think>...</think>`` blocks and any surrounding markdown fence.
      2. Tries to parse the cleaned text wholesale as a JSON object.
      3. Otherwise enumerates every balanced ``{...}`` candidate using a brace
         scanner (so nested objects are handled correctly) and parses each one.
      4. If *required_keys* is provided, returns the first candidate whose
         top-level keys are a superset of *required_keys* — this disambiguates
         responses that contain multiple unrelated JSON objects.
      5. Without *required_keys*, returns the *largest* candidate so partial
         inner objects don't shadow the intended outer one.

    Returns ``None`` when no parseable JSON object can be found.
    """
    cleaned = _sanitize(text)
    if not cleaned:
        return None

    # Fast path: the whole cleaned text is the object.
    try:
        loaded = json.loads(cleaned)
        if isinstance(loaded, dict):
            if required_keys is None or required_keys.issubset(loaded.keys()):
                return loaded
            # Whole-text parse matched a dict but lacks required keys —
            # fall through to candidate enumeration in case a nested
            # sibling object carries them.
    except (json.JSONDecodeError, ValueError):
        pass

    candidates = _candidate_dicts(cleaned)
    if not candidates:
        return None

    if required_keys is not None:
        for candidate in candidates:
            if required_keys.issubset(candidate.keys()):
                return candidate
        # No candidate satisfied the requirement — caller can retry or
        # decide to use the most complete fallback. Return None here so
        # the caller's validation gate stays in charge.
        return None

    # Default: prefer the largest candidate (outermost wins on ties via
    # JSON re-serialised length).
    candidates.sort(key=lambda obj: len(json.dumps(obj, ensure_ascii=False)), reverse=True)
    return candidates[0]


def extract_json_array(text: str) -> list | None:
    """Best-effort extraction of a JSON array from a mixed-content LLM response.

    Same hygiene chain as :func:`extract_json_object`. Returns the first
    parseable balanced ``[...]`` candidate, or ``None`` if none can be found.
    """
    cleaned = _sanitize(text)
    if not cleaned:
        return None

    try:
        loaded = json.loads(cleaned)
        if isinstance(loaded, list):
            return loaded
    except (json.JSONDecodeError, ValueError):
        pass

    candidates = _candidate_lists(cleaned)
    if not candidates:
        return None
    # Prefer the largest array — same rationale as objects.
    candidates.sort(key=lambda arr: len(json.dumps(arr, ensure_ascii=False)), reverse=True)
    return candidates[0]


def extract_first_json(text: str) -> dict | list | None:
    """Convenience: return a dict or a list, whichever appears first/larger.

    Useful for callers (like the historical ``_safe_json``) that accept
    either shape and want the most JSON-like fragment of the response.
    """
    cleaned = _sanitize(text)
    if not cleaned:
        return None

    try:
        loaded = json.loads(cleaned)
        if isinstance(loaded, (dict, list)):
            return loaded
    except (json.JSONDecodeError, ValueError):
        pass

    obj = extract_json_object(cleaned)
    arr = extract_json_array(cleaned)
    if obj is None and arr is None:
        return None
    if obj is None:
        return arr
    if arr is None:
        return obj
    # Tie-break: pick the candidate that re-serialises to more bytes —
    # the larger structure is almost always the intended payload.
    obj_len = len(json.dumps(obj, ensure_ascii=False))
    arr_len = len(json.dumps(arr, ensure_ascii=False))
    return obj if obj_len >= arr_len else arr
