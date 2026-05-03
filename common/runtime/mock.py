"""Deterministic runtime backend for unit tests."""

from __future__ import annotations

import json
import os
from typing import Callable

from common.runtime.adapter import AgenticResult, AgentRuntimeAdapter


class MockAdapter(AgentRuntimeAdapter):
    DEFAULT_RESPONSE = json.dumps(
        {
            "summary": "Mock response: task acknowledged.",
            "structured_output": {},
            "artifacts": [],
            "warnings": [],
            "next_actions": [],
        }
    )

    def run(
        self,
        prompt: str,
        context: dict | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        max_tokens: int = 4096,
    ) -> dict:
        del prompt, context, system_prompt, model, timeout, max_tokens
        raw = os.environ.get("MOCK_RUNTIME_RESPONSE", self.DEFAULT_RESPONSE)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"summary": raw, "artifacts": [], "warnings": [], "next_actions": []}
        return self.build_result(raw, structured=data, backend_used="mock")

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
        del cwd, mcp_servers, allowed_tools, disallowed_tools, max_turns, timeout, continuation
        raw = os.environ.get("MOCK_AGENTIC_RESPONSE", "Mock agentic task completed.")
        if on_progress:
            on_progress(raw[:200])
        return AgenticResult(
            success=True,
            summary=raw,
            artifacts=[],
            tool_calls=[],
            raw_output=raw,
            turns_used=1,
            backend_used="mock",
        )

    def supports_mcp(self) -> bool:
        return bool(os.environ.get("MOCK_SUPPORTS_MCP", ""))


from common.runtime.provider_registry import register_runtime  # noqa: E402

register_runtime("mock", MockAdapter)
