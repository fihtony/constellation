"""GitHub Copilot CLI runtime backend."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from typing import Callable

from common.env_utils import build_isolated_copilot_env
from common.runtime.adapter import AgenticResult, AgentRuntimeAdapter

DEFAULT_MODEL = "gpt-5-mini"


def _resolve_token() -> tuple[str, str | None]:
    if os.environ.get("COPILOT_GITHUB_TOKEN", "").strip():
        return os.environ["COPILOT_GITHUB_TOKEN"].strip(), None
    return "", None


class CopilotCliAdapter(AgentRuntimeAdapter):
    def run(
        self,
        prompt: str,
        context: dict | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        max_tokens: int = 4096,
    ) -> dict:
        token, _token_source = _resolve_token()
        binary = os.environ.get("COPILOT_CLI_BIN", "copilot").strip() or "copilot"
        if not token:
            return self.build_failure_result(
                "COPILOT_GITHUB_TOKEN is not configured; Copilot CLI cannot run.",
                warning="COPILOT_GITHUB_TOKEN is not configured.",
                backend_used="copilot-cli",
            )

        if shutil.which(binary) is None:
            return self.build_failure_result(
                f"Copilot CLI binary '{binary}' not found.",
                warning=f"Copilot CLI binary '{binary}' not found.",
                backend_used="copilot-cli",
            )

        effective_model = self.resolve_model(
            model,
            os.environ.get("AGENT_MODEL"),
            os.environ.get("COPILOT_MODEL"),
            os.environ.get("OPENAI_MODEL"),
            fallback=DEFAULT_MODEL,
        )
        full_prompt = self.build_prompt(prompt, system_prompt=system_prompt, context=context)
        cmd = [binary, "--model", effective_model, "-sp", full_prompt]
        extra_args = os.environ.get("COPILOT_CLI_ARGS", "").strip()
        if extra_args:
            cmd = [binary, *shlex.split(extra_args), "--model", effective_model, "-sp", full_prompt]
        env = build_isolated_copilot_env(token, os.environ)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return self.build_failure_result(
                f"Copilot CLI timed out after {timeout}s.",
                warning=f"Copilot CLI timed out after {timeout}s.",
                backend_used="copilot-cli",
            )
        except OSError as exc:
            return self.build_failure_result(
                f"Copilot CLI failed to start: {exc}",
                warning=f"Copilot CLI failed to start: {exc}",
                backend_used="copilot-cli",
            )

        if result.returncode != 0:
            error_text = (result.stderr or result.stdout or "").strip()
            return self.build_failure_result(
                f"Copilot CLI exited with {result.returncode}: {error_text[:300]}",
                warning=f"Copilot CLI exited with {result.returncode}.",
                backend_used="copilot-cli",
            )

        raw = (result.stdout or "").strip()
        if not raw:
            return self.build_failure_result(
                "Copilot CLI returned an empty response.",
                warning="Copilot CLI returned an empty response.",
                backend_used="copilot-cli",
            )

        return self.build_result(raw, backend_used="copilot-cli")

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
        """Copilot CLI does not support headless agentic mode.

        Use connect-agent runtime instead if you need agentic execution.
        """
        del task, system_prompt, cwd, tools, mcp_servers, allowed_tools, disallowed_tools
        del max_turns, timeout, on_progress, continuation
        raise NotImplementedError(
            "CopilotCliAdapter does not support run_agentic(). "
            "Use connect-agent runtime for agentic execution."
        )


from common.runtime.provider_registry import register_runtime  # noqa: E402

register_runtime("copilot-cli", CopilotCliAdapter)
