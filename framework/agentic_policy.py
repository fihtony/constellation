"""Shared agentic execution policy and post-step gates.

Agentic backends differ in how they expose tools. This module gives agents one
place to translate a resolved permission set into the runtime-specific tool
surface and one place to validate the reported output after each agentic step.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Any

from framework.runtime.adapter import AgenticCapabilities, AgenticResult
from framework.validation_gates import ValidationResult


@dataclass(frozen=True)
class AgenticExecutionPolicy:
    """Effective tool policy passed to an agentic runtime."""

    backend: str
    tools: list[str]
    allowed_tools: list[str]
    enforced: bool
    fail_closed_reason: str = ""


def _dedupe_tool_names(tool_names: list[str] | tuple[str, ...] | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in tool_names or []:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def _capabilities(runtime: Any) -> AgenticCapabilities:
    if runtime is not None and hasattr(runtime, "agentic_capabilities"):
        try:
            caps = runtime.agentic_capabilities()
            if isinstance(caps, AgenticCapabilities):
                return caps
            return AgenticCapabilities(
                backend=str(getattr(caps, "backend", "") or runtime.__class__.__name__),
                agentic=bool(getattr(caps, "agentic", True)),
                constellation_tools=bool(getattr(caps, "constellation_tools", False)),
                mcp_servers=bool(getattr(caps, "mcp_servers", False)),
                cwd=bool(getattr(caps, "cwd", False)),
                allowed_tools=bool(getattr(caps, "allowed_tools", False)),
                continuation=bool(getattr(caps, "continuation", False)),
                plugin_hooks=bool(getattr(caps, "plugin_hooks", False)),
            )
        except Exception:
            pass
    return AgenticCapabilities(
        backend=runtime.__class__.__name__ if runtime is not None else "unknown",
        agentic=True,
    )


def _claude_mcp_tool_name(tool_name: str) -> str:
    return f"mcp__constellation_tools__{tool_name}"


def build_agentic_execution_policy(
    runtime: Any,
    allowed_tool_names: list[str] | tuple[str, ...] | None,
) -> AgenticExecutionPolicy:
    """Build the effective policy for a run_agentic call.

    ``allowed_tool_names`` is already the resolved Constellation permission
    envelope for this agent/task. Unsupported backends still receive ``tools``
    so their own runtime contract can fail closed before launching a process.
    """
    tools = _dedupe_tool_names(allowed_tool_names)
    caps = _capabilities(runtime)
    backend = caps.backend
    if not tools:
        return AgenticExecutionPolicy(
            backend=backend,
            tools=[],
            allowed_tools=[],
            enforced=True,
        )

    if not caps.constellation_tools:
        return AgenticExecutionPolicy(
            backend=backend,
            tools=tools,
            allowed_tools=[],
            enforced=False,
            fail_closed_reason=f"{backend} does not support Constellation tools.",
        )

    if backend == "claude-code":
        if not caps.allowed_tools:
            return AgenticExecutionPolicy(
                backend=backend,
                tools=tools,
                allowed_tools=[],
                enforced=False,
                fail_closed_reason=f"{backend} does not support allowed_tools restrictions.",
            )
        return AgenticExecutionPolicy(
            backend=backend,
            tools=tools,
            allowed_tools=[_claude_mcp_tool_name(name) for name in tools],
            enforced=True,
        )

    if caps.allowed_tools:
        return AgenticExecutionPolicy(
            backend=backend,
            tools=tools,
            allowed_tools=tools[:],
            enforced=True,
        )

    return AgenticExecutionPolicy(
        backend=backend,
        tools=tools,
        allowed_tools=[],
        enforced=True,
    )


def agentic_policy_kwargs(policy: AgenticExecutionPolicy) -> dict[str, list[str]]:
    """Return keyword arguments to pass into ``runtime.run_agentic``."""
    kwargs: dict[str, list[str]] = {"tools": policy.tools}
    if policy.allowed_tools:
        kwargs["allowed_tools"] = policy.allowed_tools
    return kwargs


def _reported_tool_name(tool_call: dict[str, Any]) -> str:
    raw = str(
        tool_call.get("tool")
        or tool_call.get("name")
        or tool_call.get("tool_name")
        or ""
    ).strip()
    if raw.startswith("mcp__"):
        parts = raw.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    return raw


def validate_agentic_step_result(
    policy: AgenticExecutionPolicy,
    result: AgenticResult,
) -> ValidationResult:
    """Validate one agentic step against its effective tool policy."""
    if policy.tools and not policy.enforced:
        return ValidationResult(
            passed=False,
            gate_name="agentic_step_policy",
            feedback=policy.fail_closed_reason or "Agentic backend cannot enforce the requested tool policy.",
            details={"backend": policy.backend, "tools": policy.tools},
        )

    if not result.success:
        return ValidationResult(
            passed=False,
            gate_name="agentic_step_policy",
            feedback=result.summary or "Agentic step failed.",
            details={"backend": result.backend_used or policy.backend},
        )

    allowed = set(policy.tools)
    unauthorized: list[str] = []
    if allowed:
        for tool_call in result.tool_calls or []:
            if not isinstance(tool_call, dict):
                continue
            tool_name = _reported_tool_name(tool_call)
            if tool_name and tool_name not in allowed:
                unauthorized.append(tool_name)

    if unauthorized:
        unique = sorted(set(unauthorized))
        return ValidationResult(
            passed=False,
            gate_name="agentic_step_policy",
            feedback="Agentic step used tools outside policy: " + ", ".join(unique),
            details={"unauthorized_tools": unique, "allowed_tools": sorted(allowed)},
        )

    return ValidationResult(
        passed=True,
        gate_name="agentic_step_policy",
        feedback="Agentic step respected the effective tool policy.",
        details={"backend": result.backend_used or policy.backend, "tools": policy.tools},
    )


def record_agentic_step_gate(
    *,
    workspace_path: str,
    agent_id: str,
    task_id: str,
    step: str,
    policy: AgenticExecutionPolicy,
    result: AgenticResult,
    validation: ValidationResult,
) -> str:
    """Append a structured post-agentic validation record."""
    if not workspace_path or not agent_id or not step:
        return ""
    agent_dir = os.path.join(workspace_path, agent_id)
    try:
        os.makedirs(agent_dir, exist_ok=True)
    except OSError:
        return ""
    path = os.path.join(agent_dir, "agentic-step-gates.jsonl")
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "agent_id": agent_id,
        "task_id": task_id or "",
        "step": step,
        "backend": policy.backend,
        "policy": asdict(policy),
        "result": {
            "success": bool(result.success),
            "backend_used": result.backend_used or "",
            "turns_used": int(result.turns_used or 0),
            "summary": str(result.summary or "")[:1000],
        },
        "tool_calls": list(result.tool_calls or []),
        "validation": {
            "passed": bool(validation.passed),
            "gate_name": validation.gate_name,
            "feedback": validation.feedback,
            "details": validation.details,
        },
    }
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        return path
    except OSError:
        return ""
