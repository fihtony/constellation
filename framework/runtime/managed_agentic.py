"""Managed agentic loop for text-only runtime backends.

Some CLI backends can produce text reliably but cannot safely expose a bounded
native tool surface. This loop keeps tool execution inside Constellation:
the model emits a JSON tool request, ToolRegistry executes it under the current
PermissionEngine, and every tool call remains auditable.
"""
from __future__ import annotations

import json
import time
from typing import Any

from framework.json_extract import extract_json_object
from framework.runtime.adapter import AgenticResult


def _effective_tools(tools: list[str] | None, allowed_tools: list[str] | None) -> list[str]:
    requested = [str(name).strip() for name in (tools or []) if str(name).strip()]
    if allowed_tools:
        allowed = {str(name).strip() for name in allowed_tools if str(name).strip()}
        requested = [name for name in requested if name in allowed]
    seen: set[str] = set()
    result: list[str] = []
    for name in requested:
        if name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def _tool_schemas(tool_names: list[str]) -> list[dict[str, Any]]:
    if not tool_names:
        return []
    from framework.tools.registry import get_registry

    return get_registry().list_schemas(tool_names)


def _response_text(result: dict[str, Any]) -> str:
    return str(result.get("raw_response") or result.get("summary") or "")


def _short_text(text: str, limit: int = 500) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "...(truncated)"


def _tool_call_signature(tool_name: str, arguments: dict[str, Any]) -> tuple[str, str]:
    try:
        normalized_args = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, default=str)
    except TypeError:
        normalized_args = str(arguments or {})
    return tool_name, normalized_args


def _managed_prompt(
    *,
    task: str,
    tool_schemas: list[dict[str, Any]],
    transcript: list[dict[str, Any]],
) -> str:
    return (
        "Run the task using the managed Constellation tool loop.\n"
        "You may not use native shell, filesystem, browser, SCM, Jira, or other external tools.\n"
        "If you need a tool, return exactly one JSON object in this shape:\n"
        '{"action":"tool","tool":"tool_name","arguments":{...}}\n'
        "If the task is complete, return exactly one JSON object in this shape:\n"
        '{"action":"final","summary":"final answer"}\n\n'
        "Allowed tool schemas:\n"
        f"{json.dumps(tool_schemas, ensure_ascii=False, indent=2)}\n\n"
        "Transcript so far:\n"
        f"{json.dumps(transcript, ensure_ascii=False, indent=2)}\n\n"
        "Task:\n"
        f"{task}"
    )


def run_managed_agentic_loop(
    runtime: Any,
    *,
    backend: str,
    task: str,
    system_prompt: str | None = None,
    cwd: str | None = None,
    tools: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    max_turns: int = 50,
    timeout: int = 1800,
    on_progress: Any = None,
    plugin_manager: Any = None,
) -> AgenticResult:
    """Run a bounded JSON ReAct loop using ToolRegistry for all actions."""
    effective_tools = _effective_tools(tools, allowed_tools)
    schemas = _tool_schemas(effective_tools)
    allowed = set(effective_tools)
    transcript: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    deadline = time.time() + max(1, int(timeout or 1))
    last_text = ""

    for turn in range(1, max(1, int(max_turns or 1)) + 1):
        if time.time() >= deadline:
            return AgenticResult(
                success=False,
                summary=f"{backend} managed agentic loop timed out after {turn - 1} turns.",
                tool_calls=tool_calls,
                turns_used=turn - 1,
                backend_used=backend,
                raw_output=last_text,
            )

        prompt = _managed_prompt(task=task, tool_schemas=schemas, transcript=transcript)
        if plugin_manager:
            plugin_manager.fire_sync("before_llm_call", prompt, ctx={})
        response = runtime.run(
            prompt,
            system_prompt=system_prompt,
            cwd=cwd,
            timeout=min(120, max(1, int(deadline - time.time()))),
            disallowed_tools=["*"],
            plugin_manager=plugin_manager,
        )
        last_text = _response_text(response)
        if plugin_manager:
            plugin_manager.fire_sync("after_llm_response", last_text, ctx={})

        parsed = extract_json_object(last_text, required_keys={"action"})
        if not isinstance(parsed, dict):
            transcript.append({"role": "assistant", "content": _short_text(last_text)})
            transcript.append({
                "role": "system",
                "content": (
                    "Invalid response format. Return exactly one JSON object with "
                    "action='tool' or action='final'. Do not return prose, markdown, "
                    "or hidden reasoning."
                ),
            })
            continue

        action = str(parsed.get("action") or "").strip().lower()
        if action == "final":
            return AgenticResult(
                success=True,
                summary=str(parsed.get("summary") or "").strip() or "Done.",
                tool_calls=tool_calls,
                turns_used=turn,
                backend_used=backend,
                raw_output=last_text,
            )

        if action != "tool":
            transcript.append({"role": "assistant", "content": parsed})
            transcript.append({
                "role": "system",
                "content": "Invalid action. Return action='tool' or action='final'.",
            })
            continue

        tool_name = str(parsed.get("tool") or "").strip()
        arguments = parsed.get("arguments") if isinstance(parsed.get("arguments"), dict) else {}
        if tool_name not in allowed:
            from framework.audit_log import append_current_permission_denial

            reason = f"Managed loop rejected unauthorized tool: {tool_name}"
            append_current_permission_denial(
                operation="tool",
                reason=reason,
                metadata={"tool": tool_name, "backend": backend},
            )
            return AgenticResult(
                success=False,
                summary=reason,
                tool_calls=tool_calls,
                turns_used=turn,
                backend_used=backend,
                raw_output=last_text,
            )

        signature = _tool_call_signature(tool_name, arguments)
        repeated_identical_call = any(
            _tool_call_signature(str(call.get("tool") or ""), call.get("arguments") or {}) == signature
            for call in tool_calls
        )

        if plugin_manager:
            plugin_manager.fire_sync("before_tool_call", tool_name, arguments, ctx={})
        from framework.tools.registry import get_registry

        tool_output = get_registry().execute_sync(tool_name, arguments)
        if plugin_manager:
            plugin_manager.fire_sync("after_tool_call", tool_name, tool_output, ctx={})
        tool_call = {"tool": tool_name, "arguments": arguments, "turn": turn}
        tool_calls.append(tool_call)
        if on_progress:
            on_progress(f"Tool: {tool_name}")
        transcript.append({"role": "assistant", "content": parsed})
        transcript.append({"role": "tool", "tool": tool_name, "content": tool_output})
        if repeated_identical_call:
            transcript.append({
                "role": "system",
                "content": (
                    f"You already called {tool_name} with the same arguments and "
                    "received the tool result above. Do not repeat identical tool "
                    "calls. Use the existing result to make progress, choose a "
                    "different tool call, or return action='final'."
                ),
            })

    return AgenticResult(
        success=False,
        summary=(
            f"{backend} managed agentic loop did not return valid managed-loop JSON "
            f"after {max_turns} turns. Last response: {_short_text(last_text)}"
        ),
        tool_calls=tool_calls,
        turns_used=max_turns,
        backend_used=backend,
        raw_output=last_text,
    )
