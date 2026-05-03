"""Teams HTML normalizer — strips HTML tags and unescapes entities.

Separated from connector.py per the design in docs/compass-slack-integration-zh.md §3.1.
"""

from __future__ import annotations

import html
import re

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def normalize_text(text: str | None, text_format: str = "plain") -> str:
    """Normalize Teams message text to plain text.

    Handles:
    - HTML tag stripping when ``text_format`` is ``xml`` or ``html``
    - HTML entity unescaping (e.g. ``&amp;`` → ``&``)
    """
    if not text:
        return ""
    if text_format in ("xml", "html"):
        text = _HTML_TAG_RE.sub("", text)
    text = html.unescape(text)
    return text.strip()
