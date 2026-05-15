"""Claude Code CLI runtime adapter.

Invokes the ``claude`` subprocess (Anthropic Claude Code) for agentic tasks.
Claude handles its own tool-calling loop; we spawn it with the task prompt
and capture the final output.

Backend name: ``claude-code``
"""
from __future__ import annotations

import os
import subprocess
from shutil import which

from framework.runtime.adapter import AgenticResult, AgentRuntimeAdapter
from framework.runtime.connect_agent.transport import run_single_shot

_SINGLE_SHOT_SYSTEM = (
    "You are an expert AI agent operating inside the Constellation system. "
    "Return valid JSON when structured output is requested. Be concise."
)


def _find_claude_cli() -> str | None:
    for cmd in ("claude", "claude-code"):
        if which(cmd):
            return cmd
    return None


class ClaudeCodeAdapter(AgentRuntimeAdapter):
    """Runtime adapter that delegates agentic tasks to the ``claude`` CLI.

    Single-shot (``run``) falls back to ConnectAgent transport since
    Claude Code CLI is optimised for long agentic sessions.
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
            backend_used="claude-code",
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
        """Run a task via the claude CLI subprocess."""
        cli = _find_claude_cli()
        if not cli:
            return AgenticResult(
                success=False,
                summary="claude-code: 'claude' not found in PATH",
                backend_used="claude-code",
            )

        cmd = [cli, "--print", "--dangerously-skip-permissions"]

        full_prompt = task
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{task}"

        if mcp_servers:
            # Pass MCP server config via --mcp-config flag if supported
            import json
            import tempfile
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as tmp:
                json.dump({"mcpServers": mcp_servers}, tmp)
                mcp_config_path = tmp.name
            cmd += ["--mcp-config", mcp_config_path]

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
                    summary=f"claude exited {proc.returncode}: {stderr[:500]}",
                    backend_used="claude-code",
                )

            if on_progress:
                on_progress("claude-code completed")

            return AgenticResult(
                success=True,
                summary=stdout or "Done.",
                raw_output=stdout,
                backend_used="claude-code",
                turns_used=1,
            )
        except subprocess.TimeoutExpired:
            return AgenticResult(
                success=False,
                summary=f"claude timed out after {timeout}s",
                backend_used="claude-code",
            )
        except Exception as exc:
            return AgenticResult(
                success=False,
                summary=f"claude error: {exc}",
                backend_used="claude-code",
            )

    def supports_mcp(self) -> bool:
        return True
