"""Unified runtime contract and backend factory.

All agents that need runtime-managed reasoning should call ``get_runtime().run(...)``
instead of invoking a raw LLM or CLI command directly.

Supports two execution modes:
- ``run()``: single structured prompt → response.
- ``run_agentic()``: autonomous multi-turn execution with tools for backends
    that implement agentic control, such as ``connect-agent``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

from common.env_utils import resolve_openai_base_url


@dataclass
class AgenticResult:
    """Result of an agentic (multi-turn) execution."""

    success: bool
    summary: str
    artifacts: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    continuation: str | None = None
    raw_output: str = ""
    turns_used: int = 0
    backend_used: str = ""
    evidence: list[dict] = field(default_factory=list)
    approvals_used: list[dict] = field(default_factory=list)
    policy_profile: str = ""
    checkpoint_id: str | None = None
    verifier_summary: str | None = None


@dataclass
class AgenticCheckpoint:
    """Persisted state for resuming an agentic execution."""

    task_id: str
    provider: str
    continuation: str | None
    summary: str
    policy_hash: str = ""
    toolset_hash: str = ""
    verified_state: str | None = None
    open_questions: list[str] = field(default_factory=list)
    pending_approvals: list[dict] = field(default_factory=list)
    last_updated_at: str = ""


class AgentRuntimeAdapter(ABC):
    """Abstract base class for runtime backends.

    Backends must implement both ``run()`` (single-shot) and
    ``run_agentic()`` (multi-turn autonomous).  Backends that do not
    natively support agentic mode should raise ``NotImplementedError``
    or implement a function-calling simulation.
    """

    @abstractmethod
    def run(
        self,
        prompt: str,
        context: dict | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        max_tokens: int = 4096,
    ) -> dict:
        """Execute a prompt and return the standard runtime result contract."""
        raise NotImplementedError

    def run_agentic(
        self,
        task: str,
        *,
        system_prompt: str | None = None,
        cwd: str | None = None,
        tools: list[str] | None = None,
        mcp_servers: dict | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        max_turns: int = 50,
        timeout: int = 1800,
        on_progress: Callable[[str], None] | None = None,
        continuation: str | None = None,
    ) -> AgenticResult:
        """Autonomous multi-turn execution with tool access.

        Default implementation raises NotImplementedError.  Backends that
        support agentic mode override this method.
        """
        del task, system_prompt, cwd, tools, mcp_servers, allowed_tools, disallowed_tools
        del max_turns, timeout, on_progress, continuation
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support run_agentic(). "
            "Use a backend that supports agentic mode (e.g. connect-agent, claude-code)."
        )

    def supports_mcp(self) -> bool:
        """Return True if this backend can consume MCP servers natively."""
        return False

    @staticmethod
    def resolve_model(*candidates: str | None, fallback: str) -> str:
        for candidate in candidates:
            if candidate and str(candidate).strip():
                return str(candidate).strip()
        return fallback

    @staticmethod
    def build_prompt(
        prompt: str,
        *,
        system_prompt: str | None = None,
        context: dict | None = None,
    ) -> str:
        parts: list[str] = []
        if system_prompt:
            parts.append(system_prompt.strip())
        if context:
            parts.append("Context:\n" + json.dumps(context, ensure_ascii=False, indent=2))
        if prompt and prompt.strip():
            parts.append(prompt.strip())
        return "\n\n".join(part for part in parts if part)

    @staticmethod
    def parse_structured_output(text: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {}
        if text.startswith("```"):
            lines = text.splitlines()
            start = 1
            end = len(lines)
            while end > start and lines[end - 1].strip() in ("```", ""):
                end -= 1
            text = "\n".join(lines[start:end]).strip()
        try:
            loaded = json.loads(text)
            return loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                loaded = json.loads(match.group())
                return loaded if isinstance(loaded, dict) else {}
            except json.JSONDecodeError:
                pass
        return {}

    @classmethod
    def build_result(
        cls,
        raw: str,
        *,
        structured: dict | None = None,
        warnings: list[str] | None = None,
        backend_used: str | None = None,
    ) -> dict:
        structured = structured if structured is not None else cls.parse_structured_output(raw)
        result = {
            "summary": structured.get("summary") or (raw or "")[:500],
            "structured_output": structured,
            "artifacts": structured.get("artifacts") or [],
            "warnings": list(structured.get("warnings") or []),
            "next_actions": structured.get("next_actions") or [],
            "raw_response": raw or "",
        }
        if warnings:
            result["warnings"].extend(warnings)
        if backend_used:
            result["backend_used"] = backend_used
        return result

    @classmethod
    def build_failure_result(
        cls,
        message: str,
        *,
        warning: str | None = None,
        backend_used: str | None = None,
    ) -> dict:
        warnings = [warning] if warning else []
        return cls.build_result(
            "",
            structured={
                "summary": message,
                "artifacts": [],
                "warnings": warnings,
                "next_actions": [],
            },
            warnings=warnings,
            backend_used=backend_used,
        )


_ALIASES = {
    "copilot": "copilot-cli",
    "copilot-cli": "copilot-cli",
    "connect-agent": "connect-agent",
    "claude": "claude-code",
    "claude-code": "claude-code",
}

_INSTANCES: dict[str, AgentRuntimeAdapter] = {}


def resolve_backend_name(backend: str | None = None) -> tuple[str, str]:
    requested = (backend or os.environ.get("AGENT_RUNTIME") or "connect-agent").strip().lower()
    return requested, _ALIASES.get(requested, requested)


def _copilot_cli_status() -> dict:
    binary = os.environ.get("COPILOT_CLI_BIN", "copilot").strip() or "copilot"
    ignored_token_sources = {
        "GH_TOKEN": bool(os.environ.get("GH_TOKEN", "").strip()),
        "GITHUB_TOKEN": bool(os.environ.get("GITHUB_TOKEN", "").strip()),
    }
    token_sources = {
        "COPILOT_GITHUB_TOKEN": bool(os.environ.get("COPILOT_GITHUB_TOKEN", "").strip()),
    }
    token_configured = token_sources["COPILOT_GITHUB_TOKEN"]
    binary_available = shutil.which(binary) is not None
    return {
        "binary": binary,
        "binaryAvailable": binary_available,
        "tokenConfigured": token_configured,
        "tokenSources": token_sources,
        "ignoredTokenSources": ignored_token_sources,
        "ready": token_configured and binary_available,
    }


def _claude_code_status() -> dict:
    binary = os.environ.get("CLAUDE_CODE_BIN", "claude").strip() or "claude"
    binary_available = shutil.which(binary) is not None
    return {
        "binary": binary,
        "binaryAvailable": binary_available,
        "ready": binary_available,
    }


def summarize_runtime_configuration(backend: str | None = None) -> dict:
    requested, effective = resolve_backend_name(backend)
    summary = {
        "requestedBackend": requested,
        "effectiveBackend": effective,
    }

    if effective == "copilot-cli":
        cli_status = _copilot_cli_status()
        summary.update(
            {
                **cli_status,
                "model": AgentRuntimeAdapter.resolve_model(
                    os.environ.get("AGENT_MODEL"),
                    os.environ.get("COPILOT_MODEL"),
                    os.environ.get("OPENAI_MODEL"),
                    fallback="gpt-5-mini",
                ),
            }
        )
        if not cli_status["ready"]:
            summary["error"] = "Copilot CLI is not ready (token or binary missing)."
    elif effective == "claude-code":
        claude_status = _claude_code_status()
        summary.update(
            {
                **claude_status,
                "model": AgentRuntimeAdapter.resolve_model(
                    os.environ.get("AGENT_MODEL"),
                    os.environ.get("CLAUDE_CODE_MODEL"),
                    fallback="claude-haiku-4-5",
                ),
            }
        )
        if not claude_status["ready"]:
            summary["error"] = f"Claude Code binary '{claude_status['binary']}' is not available."
    elif effective == "connect-agent":
        summary.update(
            {
                "baseUrlConfigured": bool(os.environ.get("OPENAI_BASE_URL", "").strip()),
                "resolvedBaseUrl": resolve_openai_base_url(),
                "apiKeyConfigured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
                "model": AgentRuntimeAdapter.resolve_model(
                    os.environ.get("AGENT_MODEL"),
                    os.environ.get("OPENAI_MODEL"),
                    fallback="gpt-5-mini",
                ),
                "sandboxRoot": os.environ.get("CONNECT_AGENT_SANDBOX_ROOT", ""),
                "maxTurns": os.environ.get("CONNECT_AGENT_MAX_TURNS", "50"),
                "timeout": os.environ.get("CONNECT_AGENT_TIMEOUT", "1800"),
            }
        )
    return summary


def _load_backend_class(backend: str) -> type[AgentRuntimeAdapter]:
    if backend == "copilot-cli":
        from common.runtime.copilot_cli import CopilotCliAdapter

        return CopilotCliAdapter
    if backend == "claude-code":
        from common.runtime.claude_code import ClaudeCodeAdapter

        return ClaudeCodeAdapter
    if backend == "connect-agent":
        from common.runtime.connect_agent import ConnectAgentAdapter

        return ConnectAgentAdapter
    raise KeyError(backend)


def get_runtime(
    backend: str | None = None,
    model: str | None = None,
) -> AgentRuntimeAdapter:
    """Return a cached runtime adapter instance.

    Backend resolution priority:
    1. explicit ``backend`` argument
    2. ``AGENT_RUNTIME`` environment variable
    3. default ``connect-agent``
    """
    requested, effective_backend = resolve_backend_name(backend)

    if model:
        os.environ["AGENT_MODEL"] = model

    if effective_backend not in _INSTANCES:
        backend_class = _load_backend_class(effective_backend)
        _INSTANCES[effective_backend] = backend_class()
    return _INSTANCES[effective_backend]
