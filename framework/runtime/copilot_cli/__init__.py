"""Copilot CLI runtime adapter.

Invokes the ``gh copilot`` or ``copilot-cli`` subprocess for agentic tasks.
The CLI handles its own tool-calling loop internally; we only need to spawn
it with the task prompt and capture its stdout.

Backend name: ``copilot-cli``
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from shutil import which

from framework.runtime.adapter import (
    DEFAULT_MODEL,
    AgenticResult,
    AgentRuntimeAdapter,
)
from framework.runtime.connect_agent.transport import run_single_shot

_SINGLE_SHOT_SYSTEM = (
    "You are an expert AI agent operating inside the Constellation system. "
    "Return valid JSON when structured output is requested. Be concise."
)

_AGENTIC_SYSTEM = (
    "You are an expert autonomous agent inside the Constellation system. "
    "Follow task instructions precisely. Validate outputs before finishing."
)


def _find_copilot_cli() -> str | None:
    """Locate the gh copilot or copilot-cli executable."""
    for cmd in ("copilot-cli", "gh"):
        if which(cmd):
            return cmd
    return None


class CopilotCLIAdapter(AgentRuntimeAdapter):
    """Runtime adapter that delegates to the ``gh copilot`` CLI subprocess.

    Single-shot (``run``) calls the Copilot Connect API directly (same as
    ConnectAgentAdapter) since the CLI is optimised for agentic execution.
    ``run_agentic`` spawns the CLI subprocess with the task prompt.
    """

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
            system_prompt=system_prompt or _SINGLE_SHOT_SYSTEM,
            model=model,
            timeout=timeout,
            max_tokens=max_tokens,
            default_system=_SINGLE_SHOT_SYSTEM,
            backend_used="copilot-cli",
        )

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
        on_progress=None,
        continuation: str | None = None,
    ) -> AgenticResult:
        """Run a task via the gh copilot CLI subprocess.

        The CLI manages its own reasoning + tool loop.  We capture stdout
        and parse the final answer from the last non-empty output block.
        """
        cli = _find_copilot_cli()
        if not cli:
            return AgenticResult(
                success=False,
                summary="copilot-cli: 'gh' or 'copilot-cli' not found in PATH",
                backend_used="copilot-cli",
            )

        # Build the command.  ``gh copilot suggest`` is the interactive mode;
        # for batch execution we use a simple pipe approach.
        if cli == "gh":
            cmd = ["gh", "copilot", "suggest", "-t", "shell"]
        else:
            cmd = [cli]

        full_prompt = task
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{task}"

        try:
            proc = subprocess.run(
                cmd,
                input=full_prompt,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=timeout,
                env={**os.environ},
            )
            stdout = proc.stdout.strip()
            stderr = proc.stderr.strip()

            if proc.returncode != 0:
                return AgenticResult(
                    success=False,
                    summary=f"copilot-cli exited {proc.returncode}: {stderr[:500]}",
                    backend_used="copilot-cli",
                )

            if on_progress:
                on_progress("copilot-cli completed")

            return AgenticResult(
                success=True,
                summary=stdout or "Done.",
                raw_output=stdout,
                backend_used="copilot-cli",
                turns_used=1,
            )
        except subprocess.TimeoutExpired:
            return AgenticResult(
                success=False,
                summary=f"copilot-cli timed out after {timeout}s",
                backend_used="copilot-cli",
            )
        except Exception as exc:
            return AgenticResult(
                success=False,
                summary=f"copilot-cli error: {exc}",
                backend_used="copilot-cli",
            )

    def supports_mcp(self) -> bool:
        return True
