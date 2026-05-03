"""Slack message normalizer — converts Slack-specific tokens to plain text.

Separated from connector.py per the design in docs/compass-slack-integration-zh.md §3.1.
"""

from __future__ import annotations

import re

# Slack special-token patterns
_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")
_CHANNEL_RE = re.compile(r"<#([A-Z0-9]+)\|([^>]*)>")
_LINK_RE = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")
_BARE_LINK_RE = re.compile(r"<(https?://[^>]+)>")


def normalize_text(text: str) -> str:
    """Normalize Slack-specific tokens to plain text.

    Handles:
    - ``<@U123>`` user mentions → ``@U123``
    - ``<#C123|general>`` channel references → ``#general``
    - ``<https://example.com|label>`` labelled links → ``label (https://example.com)``
    - ``<https://example.com>`` bare links → ``https://example.com``
    """
    if not text:
        return ""
    text = _MENTION_RE.sub(r"@\1", text)
    text = _CHANNEL_RE.sub(r"#\2", text)
    text = _LINK_RE.sub(r"\2 (\1)", text)
    text = _BARE_LINK_RE.sub(r"\1", text)
    return text.strip()
