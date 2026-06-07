"""Office organize — dimension contract.

Defines the canonical set of supported grouping dimensions and a
deterministic parser that turns task metadata + user text into exactly
one dimension, or the empty string when neither source supplies a
recognized dimension. The agent MUST NOT invent a dimension; the empty
result is the signal for a structured `needs_clarification` reply.

The dimension contract is intentionally narrow. Adding a new dimension
is a one-line change here plus a corresponding tool in
``organize_by_dimension`` — never a code change buried in a prompt.

Custom dimensions
----------------

In addition to the six zero-LLM dimensions, the contract also defines
a sentinel ``__custom__`` id that signals "the user has a clear
dimension intent that does not match any of the six built-in
dimensions" (e.g. "by student name").  The custom path is a
two-phase flow: an LLM proposes a bucket plan, the user approves or
modifies, and only then does office materialize the files.  The
detection of custom intent is the *only* part that runs at
parse_dimension time so the rest of the code (and the office tools
that look up :data:`VALID_DIMENSIONS`) can stay consistent.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

# Six zero-LLM dimensions.  Adding a new one is a one-line change
# here plus a corresponding tool in ``organize_by_dimension``.
VALID_DIMENSIONS: frozenset[str] = frozenset({
    "size",
    "type",
    "created_time",
    "modified_time",
    "accessed_time",
    "filename",
})

# Sentinel id for the LLM-driven custom-dimension path.  Resolved
# when :func:`parse_dimension` detects a clear "by X" / "按X" hint
# that does not match any of the six built-in dimensions.  The
# caller is expected to read the user's natural-language hint from
# :data:`CUSTOM_DIMENSION_HINT_RE` to feed the office planner.
CUSTOM_DIMENSION: str = "__custom__"


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


# Pattern for the "by X" / "按X整理" / "X-wise" hint that signals a
# custom-dimension intent.  The captured group (1) is the raw
# dimension hint (e.g. "student name", "subject", "department") which
# the office planner feeds into its prompt.  We deliberately allow
# the hint to contain any word characters and spaces; the office
# planner decides what to do with arbitrary entity names.
_CUSTOM_DIMENSION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # English: "by X", "group by X", "organize by X"
    re.compile(r"\b(?:group(?:ing)?\s+)?by\s+([a-z][a-z0-9 _/-]{1,40})", re.IGNORECASE),
    # "X-wise": "subject-wise", "department-wise"
    re.compile(r"\b([a-z][a-z0-9 _-]{1,30})-wise\b", re.IGNORECASE),
    # Chinese: "按X整理", "按X分组", "以X划分"
    re.compile(r"按\s*([一-鿿][一-鿿0-9 _/-]{0,20})\s*(?:整理|分组|划分|归类|分)"),
    # Chinese: "以X划分"
    re.compile(r"以\s*([一-鿿][一-鿿0-9 _/-]{0,20})\s*划"),
)


def extract_custom_dimension_hint(user_text: str) -> str:
    """Pull the user's natural-language dimension hint from ``user_text``.

    Returns the first match across :data:`_CUSTOM_DIMENSION_PATTERNS`
    or ``""`` if no hint is present.  The hint is returned trimmed
    and lower-cased; the office planner is the only consumer.
    """
    text = (user_text or "").strip()
    if not text:
        return ""
    for pattern in _CUSTOM_DIMENSION_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        hint = match.group(1).strip().rstrip(",.;:?!")
        if hint and hint.lower() not in {d.replace("_", " ") for d in VALID_DIMENSIONS}:
            return hint
    return ""


def _from_metadata(metadata: Mapping[str, Any] | None) -> str:
    if not metadata:
        return ""
    raw = metadata.get("organizeGroupBy")
    if not raw or not isinstance(raw, str):
        return ""
    candidate = raw.strip().lower()
    if candidate in VALID_DIMENSIONS:
        return candidate
    # Sentinel that says "user wants the LLM custom path with the
    # hint in metadata.customDimensionHint".  Compass propagates
    # this so the office planner can pick it up.
    if candidate == CUSTOM_DIMENSION:
        return CUSTOM_DIMENSION
    return ""


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
    # No built-in match.  See if the user has a clear custom
    # dimension intent — e.g. "by student name" — and surface the
    # sentinel so the office planner can take over.
    if extract_custom_dimension_hint(user_text):
        return CUSTOM_DIMENSION
    return ""


def parse_dimension(
    metadata: Mapping[str, Any] | None,
    user_text: str,
) -> str:
    """Resolve the user-requested grouping dimension.

    Returns one of :data:`VALID_DIMENSIONS`, the sentinel
    :data:`CUSTOM_DIMENSION` (``"__custom__"``) when the user
    supplied a clear custom-dimension intent that does not match any
    built-in dimension, or ``""`` when neither the metadata nor the
    user text supplies any dimension signal at all.  The caller is
    responsible for surfacing :data:`needs_clarification` when the
    result is empty — this function never invents a default.
    """
    return _from_metadata(metadata) or _from_user_text(user_text)
