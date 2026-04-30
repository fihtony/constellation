"""Claude Code CLI runtime backend."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from typing import Callable

from common.runtime.adapter import AgenticResult, AgentRuntimeAdapter
from common.runtime.copilot_connect import CopilotConnectAdapter

DEFAULT_MODEL = "claude-haiku-4-5"

# SDK tools that conflict with Constellation's own mechanisms.
# These are disabled when running Claude Code inside a dev-agent container.
SDK_DISALLOWED_TOOLS = [
    "AskUserQuestion",   # replaced by INPUT_REQUIRED callback
    "CronCreate",        # scheduling is managed by Team Lead
    "ScheduleWakeup",    # same
    "EnterPlanMode",     # interactive UI feature; hangs in headless containers
    "ExitPlanMode",      # same
]


class ClaudeCodeAdapter(AgentRuntimeAdapter):
    def __init__(self) -> None:
        self._fallback = CopilotConnectAdapter()

    def supports_mcp(self) -> bool:
        return True

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
        # allowed_tools is superseded by the explicit `tools` list; continuation is
        # a no-op for claude-code (it uses --resume via session state files instead).
        del allowed_tools, continuation
        binary = os.environ.get("CLAUDE_CODE_BIN", "claude").strip() or "claude"
        if shutil.which(binary) is None:
            return AgenticResult(
                success=False,
                summary=f"Claude Code CLI binary '{binary}' not found.",
                backend_used="claude-code",
            )

        effective_tools = list(tools or [])
        effective_disallowed = list(disallowed_tools or []) + SDK_DISALLOWED_TOOLS

        cmd = [binary, "--print", "--dangerously-skip-permissions"]

        if effective_tools:
            cmd.extend(["--allowedTools", ",".join(effective_tools)])

        if effective_disallowed:
            cmd.extend(["--disallowedTools", ",".join(effective_disallowed)])

        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])

        mcp_config_file: str | None = None
        if mcp_servers:
            mcp_config_file = self._write_mcp_config(mcp_servers)
            cmd.extend(["--mcp-config", mcp_config_file])

        if max_turns:
            cmd.extend(["--max-turns", str(max_turns)])

        cmd.append(task)

        env = dict(os.environ)
        effective_model = self.resolve_model(
            os.environ.get("AGENT_MODEL"),
            os.environ.get("CLAUDE_CODE_MODEL"),
            fallback=DEFAULT_MODEL,
        )
        env.setdefault("ANTHROPIC_MODEL", effective_model)
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)

        run_kwargs: dict = {
            "capture_output": True,
            "text": True,
            "timeout": timeout,
            "env": env,
        }
        if cwd:
            run_kwargs["cwd"] = cwd

        try:
            result = subprocess.run(cmd, **run_kwargs)
        except subprocess.TimeoutExpired:
            return AgenticResult(
                success=False,
                summary=f"Claude Code agentic run timed out after {timeout}s.",
                backend_used="claude-code",
            )
        except OSError as exc:
            return AgenticResult(
                success=False,
                summary=f"Claude Code agentic run failed to start: {exc}",
                backend_used="claude-code",
            )
        finally:
            if mcp_config_file:
                try:
                    os.unlink(mcp_config_file)
                except OSError:
                    pass

        raw = (result.stdout or "").strip()
        success = result.returncode == 0

        if on_progress:
            on_progress(raw[:500] if raw else "(no output)")

        return AgenticResult(
            success=success,
            summary=self._extract_summary(raw),
            artifacts=self._extract_artifacts(raw),
            raw_output=raw,
            backend_used="claude-code",
        )

    @staticmethod
    def _write_mcp_config(mcp_servers: dict) -> str:
        config = {"mcpServers": mcp_servers}
        fd, path = tempfile.mkstemp(suffix=".json", prefix="mcp-config-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f)
        return path

    @staticmethod
    def _extract_summary(output: str) -> str:
        if not output:
            return ""
        lines = output.strip().splitlines()
        return lines[-1][:500] if lines else output[:500]

    @staticmethod
    def _extract_artifacts(output: str) -> list[dict]:
        artifacts = []
        for line in (output or "").splitlines():
            if line.startswith("ARTIFACT:"):
                try:
                    artifacts.append(json.loads(line[len("ARTIFACT:"):].strip()))
                except json.JSONDecodeError:
                    pass
        return artifacts


from common.runtime.provider_registry import register_runtime  # noqa: E402

register_runtime("claude-code", ClaudeCodeAdapter)
