"""Three-layer context compression engine for the Connect Agent runtime."""

from __future__ import annotations

import json
import os
import time


def estimate_tokens(messages: list[dict]) -> int:
    total_chars = 0
    for message in messages:
        content = message.get("content") or ""
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total_chars += len(part.get("text", ""))
        for tool_call in message.get("tool_calls", []):
            fn = tool_call.get("function", {})
            total_chars += len(fn.get("name", "")) + len(fn.get("arguments", ""))
    return total_chars // 4


_KEEP_RECENT = 3
_PRESERVE_TOOLS = frozenset({"read_file", "load_skill", "jira_get_ticket"})


def micro_compact(messages: list[dict]) -> None:
    tool_indices = [i for i, message in enumerate(messages) if message.get("role") == "tool"]
    if len(tool_indices) <= _KEEP_RECENT:
        return

    for index in tool_indices[:-_KEEP_RECENT]:
        tool_name = _infer_tool_name(messages, index)
        if tool_name in _PRESERVE_TOOLS:
            continue
        original_len = len(messages[index].get("content", ""))
        messages[index]["content"] = f"[Previous: used {tool_name}, {original_len} chars]"


def _infer_tool_name(messages: list[dict], tool_msg_idx: int) -> str:
    tool_call_id = messages[tool_msg_idx].get("tool_call_id", "")
    if not tool_call_id:
        return "unknown"
    for index in range(tool_msg_idx - 1, -1, -1):
        for tool_call in messages[index].get("tool_calls", []):
            if tool_call.get("id") == tool_call_id:
                return (tool_call.get("function") or {}).get("name", "unknown")
    return "unknown"


_COMPACT_SYSTEM = (
    "You are a context compressor. Summarise the conversation history below "
    "into a concise but complete status report. Preserve completed work, "
    "current work in progress, decisions, file paths, and outstanding tasks. "
    "Keep the summary under 2000 tokens."
)


def auto_compact(
    messages: list[dict],
    *,
    llm_fn: object | None = None,
    transcript_dir: str | None = None,
) -> list[dict]:
    if transcript_dir:
        _save_transcript(messages, transcript_dir)

    system_messages = [message for message in messages if message.get("role") == "system"]
    non_system = [message for message in messages if message.get("role") != "system"]

    if llm_fn is None:
        keep = non_system[-10:] if len(non_system) > 10 else non_system
        summary_text = (
            "[Context was automatically compressed. "
            f"Original conversation had {len(non_system)} messages.]"
        )
        return system_messages + [{"role": "user", "content": summary_text}] + keep

    blob_parts: list[str] = []
    for message in non_system:
        content = message.get("content", "")
        if isinstance(content, str) and content.strip():
            blob_parts.append(f"[{message.get('role', '?')}] {content[:2000]}")
    blob = "\n".join(blob_parts)

    try:
        summary = llm_fn(
            prompt=blob,
            system_prompt=_COMPACT_SYSTEM,
            max_tokens=2048,
        )
        summary_text = summary.get("raw_response") or summary.get("summary") or str(summary)
    except Exception as exc:
        summary_text = f"[Compression failed: {exc}. Keeping last 10 messages.]"
        keep = non_system[-10:] if len(non_system) > 10 else non_system
        return system_messages + [{"role": "user", "content": summary_text}] + keep

    return system_messages + [{"role": "user", "content": f"[Context Summary]\n{summary_text}"}]


def _save_transcript(messages: list[dict], transcript_dir: str) -> None:
    os.makedirs(transcript_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(transcript_dir, f"transcript_{ts}.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for message in messages:
            fh.write(json.dumps(message, ensure_ascii=False) + "\n")