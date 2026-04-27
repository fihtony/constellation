"""GitHub Copilot CLI runtime backend."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

from common.runtime.adapter import AgentRuntimeAdapter
from common.runtime.copilot_connect import CopilotConnectAdapter

DEFAULT_MODEL = "gpt-5-mini"


def _resolve_token() -> tuple[str, str | None]:
    if os.environ.get("COPILOT_GITHUB_TOKEN", "").strip():
        return os.environ["COPILOT_GITHUB_TOKEN"].strip(), None
    if os.environ.get("GH_TOKEN", "").strip():
        return os.environ["GH_TOKEN"].strip(), "GH_TOKEN"
    if os.environ.get("GITHUB_TOKEN", "").strip():
        return os.environ["GITHUB_TOKEN"].strip(), "GITHUB_TOKEN"
    return "", None


class CopilotCliAdapter(AgentRuntimeAdapter):
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
        token, token_source = _resolve_token()
        binary = os.environ.get("COPILOT_CLI_BIN", "copilot").strip() or "copilot"
        if not token:
            return self._fallback_result(
                prompt,
                context=context,
                system_prompt=system_prompt,
                model=model,
                timeout=timeout,
                max_tokens=max_tokens,
                warning="COPILOT_GITHUB_TOKEN is not configured; falling back to copilot-connect.",
            )

        if shutil.which(binary) is None:
            return self._fallback_result(
                prompt,
                context=context,
                system_prompt=system_prompt,
                model=model,
                timeout=timeout,
                max_tokens=max_tokens,
                warning=f"Copilot CLI binary '{binary}' not found; falling back to copilot-connect.",
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
        env = dict(os.environ)
        env["COPILOT_GITHUB_TOKEN"] = token

        warnings: list[str] = []
        if token_source:
            warnings.append(
                f"Using {token_source} for Copilot CLI authentication; prefer COPILOT_GITHUB_TOKEN."
            )

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
                warning=f"Copilot CLI timed out after {timeout}s; falling back to copilot-connect.",
            )
        except OSError as exc:
            return self._fallback_result(
                prompt,
                context=context,
                system_prompt=system_prompt,
                model=model,
                timeout=timeout,
                max_tokens=max_tokens,
                warning=f"Copilot CLI failed to start: {exc}; falling back to copilot-connect.",
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
                warning=f"Copilot CLI exited with {result.returncode}: {error_text[:300]}; falling back to copilot-connect.",
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
                warning="Copilot CLI returned an empty response; falling back to copilot-connect.",
            )

        return self.build_result(raw, warnings=warnings, backend_used="copilot-cli")