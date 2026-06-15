"""Unified runtime contract and backend factory.

All agents that need LLM reasoning should call ``get_runtime().run(...)``
(single-shot) or ``get_runtime().run_agentic(...)`` (multi-turn autonomous)
instead of invoking a raw LLM API directly.

Default backend: ``connect-agent`` (uses Copilot Connect / OpenAI-compatible API).
Model default: ``gpt-5-mini``.
"""
from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

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
    policy_profile: str = ""
    checkpoint_id: str | None = None
    verifier_summary: str | None = None


@dataclass(frozen=True)
class AgenticCapabilities:
    """Capabilities a runtime backend actually supports for run_agentic()."""

    backend: str
    agentic: bool
    constellation_tools: bool = False
    mcp_servers: bool = False
    cwd: bool = False
    allowed_tools: bool = False
    continuation: bool = False
    plugin_hooks: bool = False


@dataclass
class AgenticCheckpoint:
    """Persisted state for resuming an agentic execution."""

    task_id: str
    provider: str
    continuation: str | None
    summary: str
    policy_hash: str = ""
    toolset_hash: str = ""
    last_updated_at: str = ""


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_BACKEND = "claude-code"


class AgentRuntimeAdapter(ABC):
    """Abstract base class for runtime backends.

    Backends must implement ``run()`` (single-shot).  Backends that support
    autonomous tool-calling implement ``run_agentic()`` as well.
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
        plugin_manager: Any = None,
        cwd: str | None = None,
        disallowed_tools: list[str] | None = None,
    ) -> dict:
        """Execute a single prompt and return the standard result dict.

        When *plugin_manager* is provided, implementations SHOULD fire
        ``before_llm_call`` before the request and ``after_llm_response``
        after receiving the response.

        *cwd* sets the working directory for local subprocess backends
        (e.g. claude-code). Remote API backends (copilot-cli, connect-agent)
        ignore it.

        *disallowed_tools* is a safety switch for callers that want a
        pure text response and **must not** let the LLM touch the
        filesystem, the shell, or any other side-effecting native tool
        (e.g. Claude Code's ``Write``/``Edit``/``Bash``).  When set,
        local subprocess backends are expected to disable every native
        tool the LLM would otherwise have access to.  The default
        (empty / ``None``) preserves the historical behaviour so the
        change is opt-in.

        Why this exists: ``runtime.run`` is a single-shot LLM call that
        is supposed to be text-only, but local backends that shell out
        to a CLI (e.g. ``claude --print``) inherit the CLI's full tool
        surface.  An LLM asked to "write a summary" can interpret the
        word "write" as a ``Write`` tool call and drop a stray file
        into whatever directory ``cwd`` resolves to — even when the
        system prompt told it not to.  Callers that have already
        extracted the input and only need text back should pass
        ``disallowed_tools=["*"]`` (or a specific list) to make that
        contract structural instead of advisory.
        """
        ...

    def run_agentic(
        self,
        task: str,
        *,
        system_prompt: str | None = None,
        cwd: str | None = None,
        tools: list[str] | None = None,
        mcp_servers: dict | None = None,
        allowed_tools: list[str] | None = None,
        max_turns: int = 50,
        timeout: int = 1800,
        on_progress: Callable[[str], None] | None = None,
        continuation: str | None = None,
        plugin_manager: Any = None,
    ) -> AgenticResult:
        """Autonomous multi-turn execution with tool access.

        ``plugin_manager``, when provided, fires ``before_llm_call``,
        ``after_llm_response``, ``before_tool_call``, and ``after_tool_call``
        hooks around each LLM and tool invocation in the ReAct loop.

        Default: raises NotImplementedError.
        """
        capabilities = self.agentic_capabilities()
        return AgenticResult(
            success=False,
            summary=f"{capabilities.backend} does not support run_agentic().",
            backend_used=capabilities.backend,
        )

    def agentic_capabilities(self) -> AgenticCapabilities:
        """Return the backend's supported run_agentic() feature surface."""
        return AgenticCapabilities(
            backend=self.__class__.__name__,
            agentic=False,
        )

    def validate_agentic_request(
        self,
        *,
        tools: list[str] | None = None,
        mcp_servers: dict | None = None,
        allowed_tools: list[str] | None = None,
        cwd: str | None = None,
        continuation: str | None = None,
    ) -> AgenticResult | None:
        """Return a structured failure when a request exceeds backend support."""
        capabilities = self.agentic_capabilities()
        backend = capabilities.backend

        if not capabilities.agentic:
            return AgenticResult(
                success=False,
                summary=f"{backend} does not support run_agentic().",
                backend_used=backend,
            )
        if tools and not capabilities.constellation_tools:
            return AgenticResult(
                success=False,
                summary=f"{backend} run_agentic does not support Constellation tools.",
                backend_used=backend,
            )
        if mcp_servers and not capabilities.mcp_servers:
            return AgenticResult(
                success=False,
                summary=f"{backend} run_agentic does not support MCP servers.",
                backend_used=backend,
            )
        if allowed_tools and not capabilities.allowed_tools:
            return AgenticResult(
                success=False,
                summary=f"{backend} run_agentic does not support allowed_tools restrictions.",
                backend_used=backend,
            )
        if cwd and not capabilities.cwd:
            return AgenticResult(
                success=False,
                summary=f"{backend} run_agentic does not support cwd.",
                backend_used=backend,
            )
        if continuation and not capabilities.continuation:
            return AgenticResult(
                success=False,
                summary=f"{backend} run_agentic does not support continuation.",
                backend_used=backend,
            )
        return None

    def supports_mcp(self) -> bool:
        """Return True if this backend can consume MCP servers natively."""
        return self.agentic_capabilities().mcp_servers

    # -- Static helpers (kept from v1) --

    @staticmethod
    def resolve_model(*candidates: str | None, fallback: str = DEFAULT_MODEL) -> str:
        for c in candidates:
            if c and str(c).strip():
                return str(c).strip()
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
        return "\n\n".join(p for p in parts if p)

    @staticmethod
    def parse_structured_output(text: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {}
        if text.startswith("```"):
            lines = text.splitlines()
            start, end = 1, len(lines)
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
        result: dict[str, Any] = {
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


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_ALIASES: dict[str, str] = {
    "copilot": "copilot-cli",
    "copilot-cli": "copilot-cli",
    "connect-agent": "connect-agent",
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex-cli",
    "codex-cli": "codex-cli",
}

_INSTANCES: dict[str, AgentRuntimeAdapter] = {}


def resolve_backend_name(backend: str | None = None) -> tuple[str, str]:
    """Return (requested, effective) backend names."""
    requested = (backend or os.environ.get("AGENT_RUNTIME") or DEFAULT_BACKEND).strip().lower()
    return requested, _ALIASES.get(requested, requested)


def _load_backend_class(backend: str) -> type[AgentRuntimeAdapter]:
    """Lazy-import a backend adapter class.

    Adding a new runtime backend requires only:
    1. Create ``framework/runtime/<backend_name>/__init__.py`` exporting the
       adapter class.
    2. Add an entry here mapping the backend string to the import path.
    """
    if backend == "connect-agent":
        from framework.runtime.connect_agent import ConnectAgentAdapter
        return ConnectAgentAdapter
    if backend == "copilot-cli":
        from framework.runtime.copilot_cli import CopilotCLIAdapter
        return CopilotCLIAdapter
    if backend == "claude-code":
        from framework.runtime.claude_code import ClaudeCodeAdapter
        return ClaudeCodeAdapter
    if backend == "codex-cli":
        from framework.runtime.codex_cli import CodexCLIAdapter
        return CodexCLIAdapter
    raise KeyError(
        f"Unknown runtime backend: {backend!r}. "
        f"Available: connect-agent, copilot-cli, claude-code, codex-cli"
    )


def get_runtime(
    backend: str | None = None,
    model: str | None = None,
) -> AgentRuntimeAdapter:
    """Return a cached runtime adapter instance.

    Resolution priority:
    1. explicit ``backend`` argument
    2. ``AGENT_RUNTIME`` env var
    3. default ``connect-agent``
    """
    _, effective = resolve_backend_name(backend)
    if model:
        os.environ["AGENT_MODEL"] = model
    if effective not in _INSTANCES:
        backend_class = _load_backend_class(effective)
        _INSTANCES[effective] = backend_class()
    return _INSTANCES[effective]
