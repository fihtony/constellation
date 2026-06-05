"""Office organize — dimension contract.

Defines the canonical set of supported grouping dimensions and a
deterministic parser that turns task metadata + user text into exactly
one dimension, or the empty string when neither source supplies a
recognized dimension. The agent MUST NOT invent a dimension; the empty
result is the signal for a structured `needs_clarification` reply.

The dimension contract is intentionally narrow. Adding a new dimension
is a one-line change here plus a corresponding tool in
``organize_by_dimension`` — never a code change buried in a prompt.
"""
from __future__ import annotations

from typing import Any, Mapping

VALID_DIMENSIONS: frozenset[str] = frozenset({
    "size",
    "type",
    "created_time",
    "modified_time",
    "accessed_time",
    "filename",
})


# Neutral, multilingual keyword mapping. Every entry is a generic term
# that any user might reasonably type — no business-specific phrases,
# no test-case wording, no dataset references. The keys are lower-cased
# substrings; the values are the canonical dimension id.
KEYWORD_TO_DIMENSION: dict[str, str] = {
    # size
    "size": "size",
    "file size": "size",
    "by size": "size",
    "大小": "size",
    "按大小": "size",
    "按文件大小": "size",
    # type / extension
    "type": "type",
    "file type": "type",
    "extension": "type",
    "by type": "type",
    "类型": "type",
    "扩展名": "type",
    # created_time
    "created time": "created_time",
    "creation time": "created_time",
    "ctime": "created_time",
    "birthtime": "created_time",
    "创建时间": "created_time",
    "按创建时间": "created_time",
    # modified_time
    "modified time": "modified_time",
    "mtime": "modified_time",
    "last modified": "modified_time",
    "修改时间": "modified_time",
    "按修改时间": "modified_time",
    # accessed_time
    "accessed time": "accessed_time",
    "atime": "accessed_time",
    "last access": "accessed_time",
    "访问时间": "accessed_time",
    "按访问时间": "accessed_time",
    # filename
    "filename": "filename",
    "by name": "filename",
    "by filename": "filename",
    "文件名": "filename",
    "按文件名": "filename",
    "按名称": "filename",
}


def _from_metadata(metadata: Mapping[str, Any] | None) -> str:
    if not metadata:
        return ""
    raw = metadata.get("organizeGroupBy")
    if not raw or not isinstance(raw, str):
        return ""
    candidate = raw.strip().lower()
    return candidate if candidate in VALID_DIMENSIONS else ""


def _from_user_text(user_text: str) -> str:
    if not user_text:
        return ""
    text = user_text.lower()
    # Order matters only for the test of "specific phrase wins over
    # generic"; the dict already gives specific phrases a longer
    # substring (e.g. "file size" before "size"). We still iterate
    # longest-first to be explicit.
    for needle in sorted(KEYWORD_TO_DIMENSION, key=len, reverse=True):
        if needle in text:
            return KEYWORD_TO_DIMENSION[needle]
    return ""


def parse_dimension(
    metadata: Mapping[str, Any] | None,
    user_text: str,
) -> str:
    """Resolve the user-requested grouping dimension.

    Returns one of ``VALID_DIMENSIONS`` or ``""`` when neither the
    metadata nor the user text supplies a recognized dimension. The
    caller is responsible for surfacing ``needs_clarification`` when
    the result is empty — this function never invents a default.
    """
    return _from_metadata(metadata) or _from_user_text(user_text)
