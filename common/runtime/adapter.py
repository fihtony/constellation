"""Unified agentic runtime contract and backend factory.

All agents that need agentic reasoning should call ``get_runtime().run(...)``
instead of invoking a raw LLM or CLI command directly.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from abc import ABC, abstractmethod

from common.env_utils import env_flag


class AgentRuntimeAdapter(ABC):
    """Abstract base class for runtime backends."""

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
    "copilot-connect": "copilot-connect",
    "claude": "claude-code",
    "claude-code": "claude-code",
    "mock": "mock",
}

_INSTANCES: dict[str, AgentRuntimeAdapter] = {}


def resolve_backend_name(backend: str | None = None) -> tuple[str, str]:
    requested = (backend or os.environ.get("AGENT_RUNTIME") or "copilot-connect").strip().lower()
    return requested, _ALIASES.get(requested, "copilot-connect")


def summarize_runtime_configuration(backend: str | None = None) -> dict:
    requested, effective = resolve_backend_name(backend)
    summary = {
        "requestedBackend": requested,
        "effectiveBackend": effective,
        "allowMockFallback": env_flag("ALLOW_MOCK_FALLBACK", default=True),
    }

    if effective == "copilot-cli":
        binary = os.environ.get("COPILOT_CLI_BIN", "copilot").strip() or "copilot"
        summary.update(
            {
                "binary": binary,
                "binaryAvailable": shutil.which(binary) is not None,
                "tokenConfigured": any(
                    os.environ.get(name, "").strip()
                    for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
                ),
                "tokenSources": {
                    "COPILOT_GITHUB_TOKEN": bool(os.environ.get("COPILOT_GITHUB_TOKEN", "").strip()),
                    "GH_TOKEN": bool(os.environ.get("GH_TOKEN", "").strip()),
                    "GITHUB_TOKEN": bool(os.environ.get("GITHUB_TOKEN", "").strip()),
                },
                "model": AgentRuntimeAdapter.resolve_model(
                    os.environ.get("AGENT_MODEL"),
                    os.environ.get("COPILOT_MODEL"),
                    os.environ.get("OPENAI_MODEL"),
                    fallback="gpt-5-mini",
                ),
            }
        )
    elif effective == "claude-code":
        binary = os.environ.get("CLAUDE_CODE_BIN", "claude").strip() or "claude"
        summary.update(
            {
                "binary": binary,
                "binaryAvailable": shutil.which(binary) is not None,
                "model": AgentRuntimeAdapter.resolve_model(
                    os.environ.get("AGENT_MODEL"),
                    os.environ.get("CLAUDE_CODE_MODEL"),
                    fallback="claude-haiku-4-5",
                ),
            }
        )
    elif effective == "copilot-connect":
        summary.update(
            {
                "baseUrlConfigured": bool(os.environ.get("OPENAI_BASE_URL", "").strip()),
                "apiKeyConfigured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
                "model": AgentRuntimeAdapter.resolve_model(
                    os.environ.get("AGENT_MODEL"),
                    os.environ.get("OPENAI_MODEL"),
                    fallback="gpt-5-mini",
                ),
            }
        )
    elif effective == "mock":
        summary.update(
            {
                "model": AgentRuntimeAdapter.resolve_model(
                    os.environ.get("AGENT_MODEL"),
                    fallback="mock",
                ),
                "customResponseConfigured": bool(os.environ.get("MOCK_RUNTIME_RESPONSE", "").strip()),
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
    if backend == "copilot-connect":
        from common.runtime.copilot_connect import CopilotConnectAdapter

        return CopilotConnectAdapter
    if backend == "mock":
        from common.runtime.mock import MockAdapter

        return MockAdapter
    raise KeyError(backend)


def get_runtime(
    backend: str | None = None,
    model: str | None = None,
) -> AgentRuntimeAdapter:
    """Return a cached runtime adapter instance.

    Backend resolution priority:
    1. explicit ``backend`` argument
    2. ``AGENT_RUNTIME`` environment variable
    3. default ``copilot-connect``
    """
    requested, effective_backend = resolve_backend_name(backend)

    if requested not in _ALIASES:
        print(
            f"[runtime] Unknown backend '{requested}', falling back to '{effective_backend}'."
        )

    if model:
        os.environ["AGENT_MODEL"] = model

    if effective_backend not in _INSTANCES:
        backend_class = _load_backend_class(effective_backend)
        _INSTANCES[effective_backend] = backend_class()
    return _INSTANCES[effective_backend]