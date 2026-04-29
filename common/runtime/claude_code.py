"""Claude Code CLI runtime backend."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

from common.runtime.adapter import AgentRuntimeAdapter
from common.runtime.copilot_connect import CopilotConnectAdapter

DEFAULT_MODEL = "claude-haiku-4-5"


class ClaudeCodeAdapter(AgentRuntimeAdapter):
    def __init__(self) -> None:
        self._fallback = CopilotConnectAdapter()

    def _fallback_result(
        self,
        prompt: str,
        *,
        context: dict | None,
        system_prompt: str | None,
        model: str | None,
        timeout: int,
        max_tokens: int,
        warning: str,
    ) -> dict:
        result = self._fallback.run(
            prompt,
            context=context,
            system_prompt=system_prompt,
            model=model,
            timeout=timeout,
            max_tokens=max_tokens,
        )
        result.setdefault("warnings", []).insert(0, warning)
        result["backend_used"] = result.get("backend_used") or "copilot-connect"
        return result

    def run(
        self,
        prompt: str,
        context: dict | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        max_tokens: int = 4096,
    ) -> dict:
        binary = os.environ.get("CLAUDE_CODE_BIN", "claude").strip() or "claude"
        if shutil.which(binary) is None:
            return self._fallback_result(
                prompt,
                context=context,
                system_prompt=system_prompt,
                model=model,
                timeout=timeout,
                max_tokens=max_tokens,
                warning=f"Claude Code CLI binary '{binary}' not found; falling back to copilot-connect.",
            )

        effective_model = self.resolve_model(
            model,
            os.environ.get("AGENT_MODEL"),
            os.environ.get("CLAUDE_CODE_MODEL"),
            fallback=DEFAULT_MODEL,
        )
        full_prompt = self.build_prompt(prompt, system_prompt=system_prompt, context=context)
        extra_args = os.environ.get("CLAUDE_CODE_ARGS", "").strip()
        if extra_args:
            cmd = [binary, *shlex.split(extra_args), full_prompt]
        else:
            cmd = [binary, "-p", full_prompt]
        env = dict(os.environ)
        env.setdefault("ANTHROPIC_MODEL", effective_model)
        # Strip generic GitHub tokens — Claude Code must not fall back to host GitHub
        # credentials. All GitHub auth inside the system goes through SCM_TOKEN or
        # COPILOT_GITHUB_TOKEN defined explicitly in .env files.
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return self._fallback_result(
                prompt,
                context=context,
                system_prompt=system_prompt,
                model=model,
                timeout=timeout,
                max_tokens=max_tokens,
                warning=f"Claude Code CLI timed out after {timeout}s; falling back to copilot-connect.",
            )
        except OSError as exc:
            return self._fallback_result(
                prompt,
                context=context,
                system_prompt=system_prompt,
                model=model,
                timeout=timeout,
                max_tokens=max_tokens,
                warning=f"Claude Code CLI failed to start: {exc}; falling back to copilot-connect.",
            )

        if result.returncode != 0:
            error_text = (result.stderr or result.stdout or "").strip()
            return self._fallback_result(
                prompt,
                context=context,
                system_prompt=system_prompt,
                model=model,
                timeout=timeout,
                max_tokens=max_tokens,
                warning=f"Claude Code CLI exited with {result.returncode}: {error_text[:300]}; falling back to copilot-connect.",
            )

        raw = (result.stdout or "").strip()
        if not raw:
            return self._fallback_result(
                prompt,
                context=context,
                system_prompt=system_prompt,
                model=model,
                timeout=timeout,
                max_tokens=max_tokens,
                warning="Claude Code CLI returned an empty response; falling back to copilot-connect.",
            )

        return self.build_result(raw, backend_used="claude-code")