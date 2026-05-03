"""Legacy Copilot Connect backend — thin wrapper kept for backward compatibility.

New code should use ``connect-agent`` directly.  This module only provides
single-shot ``run()`` and raises NotImplementedError for ``run_agentic()``.
"""

from __future__ import annotations

from common.runtime.adapter import AgentRuntimeAdapter
from common.runtime.connect_agent.transport import run_single_shot


class CopilotConnectAdapter(AgentRuntimeAdapter):
    """Backward-compatible single-shot adapter.

    This is NOT registered as a selectable agentic runtime.
    It exists only so that code importing ``CopilotConnectAdapter`` still works.
    """

    DEFAULT_SYSTEM = (
        "You are an expert software engineering agent. "
        "When asked for structured data, return valid JSON. "
        "Be concise and precise."
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
        return run_single_shot(
            prompt,
            context=context,
            system_prompt=system_prompt,
            model=model,
            timeout=timeout,
            max_tokens=max_tokens,
            default_system=self.DEFAULT_SYSTEM,
            backend_used="copilot-connect",
        )
