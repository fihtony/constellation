"""Managed agentic loop for text-only runtime backends.

Some CLI backends can produce text reliably but cannot safely expose a bounded
native tool surface. This loop keeps tool execution inside Constellation:
the model emits a JSON tool request, ToolRegistry executes it under the current
PermissionEngine, and every tool call remains auditable.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from framework.json_extract import extract_json_object, strip_think_blocks
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


def _transcript_content(value: Any, limit: int = 8000) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    return _short_text(text, limit=limit)


def _resolve_under_cwd(value: Any, cwd: str | None) -> Any:
    """Resolve relative tool paths against the managed-loop repository cwd."""
    if not cwd or not isinstance(value, str) or not value.strip():
        return value
    if os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(cwd, value))


def _canonical_tool_name(tool_name: str) -> str:
    """Map common CLI/model tool aliases to Constellation tool names.

    Managed-loop backends all see the same tool schemas, but models trained on
    coding CLIs often emit familiar native names such as ``bash`` or
    ``read_multiple_files``.  Aliases never broaden permissions: callers still
    have to allow the canonical Constellation tool before execution proceeds.
    """
    normalized = str(tool_name or "").strip()
    alias = normalized.lower().replace("-", "_").replace(".", "_")
    aliases = {
        "bash": "run_command",
        "shell": "run_command",
        "terminal": "run_command",
        "run_shell_command": "run_command",
        "execute_command": "run_command",
        "read": "read_file",
        "readfile": "read_file",
        "read_files": "read_file",
        "read_multiple_files": "read_file",
        "multi_read": "read_file",
        "write": "write_file",
        "writefile": "write_file",
        "edit": "edit_file",
        "replace": "edit_file",
        "search": "search_code",
        "search_files": "search_code",
        "grep_search": "grep",
        "list_files": "glob",
        "find_files": "glob",
    }
    return aliases.get(alias, normalized)


def _tool_call_blocks(text: str) -> list[str]:
    """Return explicit tool-call blocks emitted by CLI-style models."""
    cleaned = strip_think_blocks(str(text or ""))
    blocks = re.findall(
        r"\[TOOL_CALL\](.*?)(?:\[/TOOL_CALL\]|$)",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return [block.strip() for block in blocks if block.strip()]


def _tool_args_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    for key in ("arguments", "args", "input", "parameters"):
        value = candidate.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _parse_json_tool_candidate(text: str) -> dict[str, Any] | None:
    """Parse a JSON-ish tool call that omits the managed ``action`` field."""
    candidate = extract_json_object(text, required_keys={"tool"})
    if not isinstance(candidate, dict):
        candidate = extract_json_object(text, required_keys={"name"})
    if not isinstance(candidate, dict):
        return None

    tool_name = str(candidate.get("tool") or candidate.get("name") or "").strip()
    if not tool_name:
        return None
    return {
        "action": "tool",
        "tool": tool_name,
        "arguments": _tool_args_from_candidate(candidate),
    }


def _parse_loose_tool_call(text: str) -> dict[str, Any] | None:
    """Parse non-JSON tool-call snippets commonly emitted by CLI adapters.

    Example accepted shape::

        [TOOL_CALL]
        {tool => "read_file", args => { --path "/repo/src/App.tsx" }}
        [/TOOL_CALL]
    """
    blocks = _tool_call_blocks(text) or [strip_think_blocks(str(text or ""))]
    for block in blocks:
        parsed_json = _parse_json_tool_candidate(block)
        if parsed_json:
            return parsed_json

        tool_match = re.search(
            r"\btool\b\s*(?:=>|:)\s*['\"]?([A-Za-z0-9_.-]+)['\"]?",
            block,
        )
        if not tool_match:
            continue
        tool_name = tool_match.group(1)
        arguments: dict[str, Any] = {}

        for key, quoted, single_quoted, bare in re.findall(
            r"--([A-Za-z_][A-Za-z0-9_-]*)\s+(?:\"([^\"]*)\"|'([^']*)'|([^,\s}]+))",
            block,
        ):
            arguments[key.replace("-", "_")] = quoted or single_quoted or bare

        for key, quoted, single_quoted, bare in re.findall(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\b\s*(?:=>|:)\s*(?:\"([^\"]*)\"|'([^']*)'|([^,\s}]+))",
            block,
        ):
            if key in {"tool", "args", "arguments"}:
                continue
            arguments.setdefault(key, quoted or single_quoted or bare)

        return {"action": "tool", "tool": tool_name, "arguments": arguments}
    return None


def _parse_managed_response(text: str) -> dict[str, Any] | None:
    parsed = extract_json_object(text, required_keys={"action"})
    if isinstance(parsed, dict):
        return parsed

    for block in _tool_call_blocks(text):
        parsed = _parse_json_tool_candidate(block) or _parse_loose_tool_call(block)
        if parsed:
            return parsed

    return _parse_json_tool_candidate(text) or _parse_loose_tool_call(text)


def _coerce_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    args = dict(arguments or {})
    canonical = _canonical_tool_name(tool_name)
    if canonical == "run_command":
        for key in ("cmd", "shell_command", "command_line"):
            if key in args and "command" not in args:
                args["command"] = args.pop(key)
    if canonical in {"search_code", "grep"}:
        for key in ("query", "text"):
            if key in args and "pattern" not in args:
                args["pattern"] = args.pop(key)
    if canonical == "glob":
        if "path" in args and "root" not in args:
            args["root"] = args.pop("path")
            args.setdefault("pattern", "**/*")
        for key in ("query",):
            if key in args and "pattern" not in args:
                args["pattern"] = args.pop(key)
    return args


def _apply_default_cwd(
    tool_name: str,
    arguments: dict[str, Any],
    cwd: str | None,
) -> dict[str, Any]:
    """Apply native-CLI cwd semantics to Constellation-managed tools.

    Native agentic CLIs usually run file and shell tools relative to their
    configured working directory. The managed loop executes tools from the
    Constellation process, so it must resolve relative paths explicitly to keep
    backend behavior consistent.
    """
    if not cwd:
        return arguments

    args = dict(arguments or {})
    if tool_name == "run_command":
        if not args.get("cwd"):
            args["cwd"] = cwd
        else:
            args["cwd"] = _resolve_under_cwd(args.get("cwd"), cwd)
    elif tool_name in {"read_file", "write_file", "edit_file"}:
        if args.get("path"):
            args["path"] = _resolve_under_cwd(args.get("path"), cwd)
    elif tool_name in {"search_code", "grep"}:
        if not args.get("path") or str(args.get("path")) == ".":
            args["path"] = cwd
        else:
            args["path"] = _resolve_under_cwd(args.get("path"), cwd)
    elif tool_name == "glob":
        if not args.get("root") or str(args.get("root")) == ".":
            args["root"] = cwd
        else:
            args["root"] = _resolve_under_cwd(args.get("root"), cwd)
    return args


def _expand_tool_requests(
    tool_name: str,
    arguments: dict[str, Any],
) -> list[tuple[str, dict[str, Any], str]]:
    """Return canonical tool calls as ``(tool, args, requested_tool)`` tuples."""
    canonical = _canonical_tool_name(tool_name)
    args = _coerce_tool_arguments(tool_name, arguments)
    requested = str(tool_name or "").strip()

    if requested.lower().replace("-", "_") in {"read_multiple_files", "read_files", "multi_read"}:
        paths = args.get("paths") or args.get("files") or args.get("path")
        if isinstance(paths, str):
            paths = [paths]
        if isinstance(paths, list) and paths:
            expanded: list[tuple[str, dict[str, Any], str]] = []
            for path in paths:
                path_text = str(path or "").strip()
                if path_text:
                    expanded.append(("read_file", {"path": path_text}, requested))
            if expanded:
                return expanded

    return [(canonical, args, requested)]


def _tool_call_signature(tool_name: str, arguments: dict[str, Any]) -> tuple[str, str]:
    try:
        normalized_args = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, default=str)
    except TypeError:
        normalized_args = str(arguments or {})
    return tool_name, normalized_args


def _tool_error_text(tool_output: Any) -> str:
    if not isinstance(tool_output, str):
        return ""
    try:
        parsed = json.loads(tool_output)
    except (TypeError, ValueError):
        return ""
    if isinstance(parsed, dict) and parsed.get("error"):
        return str(parsed.get("error") or "")
    return ""


def _is_permission_denial(error_text: str) -> bool:
    lowered = str(error_text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "not permitted",
            "permission denied",
            "unauthorized",
            "rejected",
        )
    )


def _permission_recovery_message(
    tool_name: str,
    arguments: dict[str, Any],
    error_text: str,
) -> str:
    command = str(arguments.get("command") or "") if isinstance(arguments, dict) else ""
    command_hint = ""
    if tool_name == "run_command":
        command_hint = (
            " Do not retry denied commands, shell pipelines, redirections, "
            "background jobs, or chained commands. Run one permitted command "
            "at a time."
        )
    if "npm create" in command.lower() or "create vite" in command.lower():
        command_hint += (
            " For project scaffolding, create the required files with "
            "write_file/edit_file instead of using interactive or remote "
            "project generators."
        )
    return (
        f"{tool_name} was denied by Constellation permissions: {error_text}."
        f"{command_hint} Choose an allowed tool call that makes concrete progress."
    )


def _managed_prompt(
    *,
    task: str,
    tool_schemas: list[dict[str, Any]],
    transcript: list[dict[str, Any]],
    cwd: str | None = None,
) -> str:
    cwd_note = (
        f"Relative file paths and command working directories default to: {cwd}\n"
        if cwd else ""
    )
    return (
        "Run the task using the managed Constellation tool loop.\n"
        "You may not use native shell, filesystem, browser, SCM, Jira, or other external tools.\n"
        "Use only the exact tool names from the allowed schemas below. Do not invent aliases.\n"
        f"{cwd_note}"
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

        prompt = _managed_prompt(
            task=task,
            tool_schemas=schemas,
            transcript=transcript,
            cwd=cwd,
        )
        if on_progress:
            on_progress(f"{backend} managed turn {turn}/{max(1, int(max_turns or 1))}")
        if plugin_manager:
            plugin_manager.fire_sync("before_llm_call", prompt, ctx={})
        try:
            response = runtime.run(
                prompt,
                system_prompt=system_prompt,
                cwd=cwd,
                timeout=min(120, max(1, int(deadline - time.time()))),
                disallowed_tools=["*"],
                plugin_manager=plugin_manager,
            )
        except Exception as exc:  # noqa: BLE001
            return AgenticResult(
                success=False,
                summary=f"{backend} managed agentic loop failed on turn {turn}: {exc}",
                tool_calls=tool_calls,
                turns_used=turn,
                backend_used=backend,
                raw_output=last_text,
            )
        last_text = _response_text(response)
        if plugin_manager:
            plugin_manager.fire_sync("after_llm_response", last_text, ctx={})

        parsed = _parse_managed_response(last_text)
        if not isinstance(parsed, dict):
            transcript.append({"role": "assistant", "content": _short_text(last_text)})
            transcript.append({
                "role": "system",
                "content": (
                    "Invalid response format. Return exactly one JSON object with "
                    "action='tool' or action='final'. Do not return prose, markdown, "
                    "hidden reasoning, or [TOOL_CALL] wrappers."
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

        tool_name = str(parsed.get("tool") or parsed.get("name") or "").strip()
        arguments = parsed.get("arguments") if isinstance(parsed.get("arguments"), dict) else {}
        if not arguments:
            arguments = _tool_args_from_candidate(parsed)

        expanded_requests = _expand_tool_requests(tool_name, arguments)
        unauthorized = [
            canonical for canonical, _args, _requested in expanded_requests
            if canonical not in allowed
        ]
        if unauthorized:
            from framework.audit_log import append_current_permission_denial

            reason = f"Managed loop rejected unauthorized tool: {tool_name}"
            append_current_permission_denial(
                operation="tool",
                reason=reason,
                metadata={"tool": tool_name, "canonical_tools": unauthorized, "backend": backend},
            )
            return AgenticResult(
                success=False,
                summary=reason,
                tool_calls=tool_calls,
                turns_used=turn,
                backend_used=backend,
                raw_output=last_text,
            )

        from framework.tools.registry import get_registry

        tool_outputs: list[dict[str, Any]] = []
        repeated_messages: list[str] = []
        permission_messages: list[str] = []
        for canonical_tool_name, canonical_args, requested_tool_name in expanded_requests:
            canonical_args = _apply_default_cwd(canonical_tool_name, canonical_args, cwd)
            signature = _tool_call_signature(canonical_tool_name, canonical_args)
            repeated_identical_call = any(
                _tool_call_signature(str(call.get("tool") or ""), call.get("arguments") or {}) == signature
                for call in tool_calls
            )

            if plugin_manager:
                plugin_manager.fire_sync("before_tool_call", canonical_tool_name, canonical_args, ctx={})

            tool_output = get_registry().execute_sync(canonical_tool_name, canonical_args)
            if plugin_manager:
                plugin_manager.fire_sync("after_tool_call", canonical_tool_name, tool_output, ctx={})
            tool_call = {
                "tool": canonical_tool_name,
                "arguments": canonical_args,
                "turn": turn,
            }
            if requested_tool_name and requested_tool_name != canonical_tool_name:
                tool_call["requested_tool"] = requested_tool_name
            tool_calls.append(tool_call)
            tool_outputs.append({
                "tool": canonical_tool_name,
                "arguments": canonical_args,
                "content": _transcript_content(tool_output),
            })
            error_text = _tool_error_text(tool_output)
            if _is_permission_denial(error_text):
                permission_messages.append(
                    _permission_recovery_message(canonical_tool_name, canonical_args, error_text)
                )
            if on_progress:
                on_progress(f"Tool: {canonical_tool_name}")
            if repeated_identical_call:
                repeated_messages.append(
                    f"You already called {canonical_tool_name} with the same arguments and "
                    "received the tool result above. Do not repeat identical tool calls."
                )

        tool_output = (
            tool_outputs[0]["content"] if len(tool_outputs) == 1
            else json.dumps({"results": tool_outputs}, ensure_ascii=False)
        )
        transcript.append({"role": "assistant", "content": parsed})
        transcript.append({
            "role": "tool",
            "tool": expanded_requests[0][0] if len(expanded_requests) == 1 else "multiple",
            "content": tool_output,
        })
        system_messages = repeated_messages + permission_messages
        if system_messages:
            transcript.append({
                "role": "system",
                "content": " ".join(system_messages) + (
                    " Use the existing result to make progress, choose a different "
                    "tool call, or return action='final'."
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
