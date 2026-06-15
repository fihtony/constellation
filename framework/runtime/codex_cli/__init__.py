"""Codex CLI runtime adapter.

Invokes the ``codex`` subprocess (OpenAI Codex CLI) for agentic tasks.
Codex CLI manages its own reasoning loop; we spawn it with the task prompt.

Backend name: ``codex-cli``
"""
from __future__ import annotations

import os
import subprocess
from shutil import which

from framework.runtime.adapter import AgenticCapabilities, AgenticResult, AgentRuntimeAdapter
from framework.runtime.cli_prompt import cli_prompt_argument
from framework.runtime.connect_agent.transport import run_single_shot

_SINGLE_SHOT_SYSTEM = (
    "You are an expert AI agent operating inside the Constellation system. "
    "Return valid JSON when structured output is requested. Be concise."
)


def _find_codex_cli() -> str | None:
    for cmd in ("codex", "codex-cli"):
        if which(cmd):
            return cmd
    return None


class CodexCLIAdapter(AgentRuntimeAdapter):
    """Runtime adapter delegating to the ``codex`` CLI (OpenAI Codex)."""

    def run(
        self,
        prompt: str,
        context: dict | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        max_tokens: int = 4096,
        plugin_manager=None,
        cwd: str | None = None,
        disallowed_tools: list[str] | None = None,
    ) -> dict:
        # ``disallowed_tools`` is honoured by the local-subprocess
        # backends (claude-code).  The remote API backends (this one,
        # connect-agent, copilot-cli) do not pass native tools to the
        # LLM in single-shot mode, so the flag is a structural no-op
        # here — but we still accept it so the call site has one
        # contract across every backend.
        return run_single_shot(
            prompt,
            context=context,
            system_prompt=system_prompt or _SINGLE_SHOT_SYSTEM,
            model=model,
            timeout=timeout,
            max_tokens=max_tokens,
            default_system=_SINGLE_SHOT_SYSTEM,
            backend_used="codex-cli",
            cwd=cwd,
            disallowed_tools=disallowed_tools,
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
        plugin_manager=None,
    ) -> AgenticResult:
        """Run a task via the codex CLI subprocess."""
        unsupported = self.validate_agentic_request(
            tools=tools,
            mcp_servers=mcp_servers,
            allowed_tools=allowed_tools,
            cwd=cwd,
            continuation=continuation,
        )
        if unsupported:
            return unsupported

        cli = _find_codex_cli()
        if not cli:
            return AgenticResult(
                success=False,
                summary="codex-cli: 'codex' not found in PATH",
                backend_used="codex-cli",
            )

        # codex CLI: `codex --approval-mode full-auto -q "<prompt>"`
        full_prompt = task
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{task}"

        try:
            with cli_prompt_argument(full_prompt, backend="codex-cli") as prompt_arg:
                cmd = [cli, "--approval-mode", "full-auto", "-q", prompt_arg]
                proc = subprocess.run(
                    cmd,
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
                    summary=f"codex exited {proc.returncode}: {stderr[:500]}",
                    backend_used="codex-cli",
                )

            if on_progress:
                on_progress("codex-cli completed")

            return AgenticResult(
                success=True,
                summary=stdout or "Done.",
                raw_output=stdout,
                backend_used="codex-cli",
                turns_used=1,
            )
        except subprocess.TimeoutExpired:
            return AgenticResult(
                success=False,
                summary=f"codex timed out after {timeout}s",
                backend_used="codex-cli",
            )
        except Exception as exc:
            return AgenticResult(
                success=False,
                summary=f"codex error: {exc}",
                backend_used="codex-cli",
            )

    def supports_mcp(self) -> bool:
        return self.agentic_capabilities().mcp_servers

    def agentic_capabilities(self) -> AgenticCapabilities:
        return AgenticCapabilities(
            backend="codex-cli",
            agentic=True,
            constellation_tools=False,
            mcp_servers=False,
            cwd=True,
            allowed_tools=False,
            continuation=False,
            plugin_hooks=False,
        )
