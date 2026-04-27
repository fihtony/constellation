"""Deterministic runtime backend for unit tests."""

from __future__ import annotations

import json
import os

from common.runtime.adapter import AgentRuntimeAdapter


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