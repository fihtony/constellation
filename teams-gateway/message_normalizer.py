"""Normalize incoming Teams messages (strip HTML, decode entities, etc.)."""

from __future__ import annotations

import html
import re

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def normalize_message(text: str | None, text_format: str = "plain") -> str:
    """Return clean plain text from a Teams Activity.text field.

    1. If textFormat is 'xml' or 'html', strip HTML tags.
    2. Decode HTML entities (&amp; -> &, etc.).
    3. Strip leading/trailing whitespace.
    4. Preserve emoji (Unicode chars pass through unchanged).
    """
    if not text:
        return ""
    if text_format in ("xml", "html"):
        text = _HTML_TAG_RE.sub("", text)
    text = html.unescape(text)
    return text.strip()
