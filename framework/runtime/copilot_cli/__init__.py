"""Copilot CLI runtime adapter.

Invokes the standalone ``@github/copilot`` npm binary for agentic tasks.
The CLI handles its own tool-calling loop internally; we only need to
spawn it with the task prompt and capture its stdout.

We deliberately do NOT fall back to the deprecated ``gh copilot`` path.
That path reads the host's ``~/.config/gh/hosts.yml`` (the developer's
``gh auth`` token) and would silently use it as the GitHub credential.
The standalone binary, in contrast, is BYOK-safe: in custom-provider
mode (the only mode we support) it does not consult any local GitHub
auth store, so the only credentials that can reach the subprocess are
the ``COPILOT_*`` env vars in ``config/.env``.

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
    AgenticCapabilities,
    AgenticResult,
    AgentRuntimeAdapter,
)
from framework.runtime.cli_prompt import cli_prompt_argument
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
    """Locate the copilot CLI executable.

    Only the standalone ``@github/copilot`` binary is supported.
    We deliberately do NOT fall back to ``gh copilot`` (the GitHub
    CLI's copilot extension) because that path reads the host's
    ``~/.config/gh/hosts.yml`` — the developer's local GitHub
    auth — and would silently use it as the credential.

    Lookup order:
      1. ``copilot``    — the npm binary name as installed by
                          ``npm install -g @github/copilot``.
      2. ``copilot-cli`` — alternate name some installs use.

    Returns the executable name, or ``None`` if neither is on
    ``PATH``.
    """
    for cmd in ("copilot", "copilot-cli"):
        if which(cmd):
            return cmd
    return None


class CopilotCLIAdapter(AgentRuntimeAdapter):
    """Runtime adapter that delegates to the standalone ``copilot`` CLI.

    Single-shot (``run``) calls the Connect Agent transport directly
    (same path as ``ConnectAgentAdapter``) since the CLI is optimised
    for agentic execution.  ``run_agentic`` spawns the standalone
    ``copilot`` subprocess with the task prompt.  The deprecated
    ``gh copilot`` path is intentionally not supported — see
    :func:`_find_copilot_cli` for the security rationale.
    """

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
        # connect-agent, codex-cli) do not pass native tools to the
        # LLM in single-shot mode, so the flag is a structural no-op
        # here — but we still accept it so the call site has one
        # contract across every backend.
        provider_base_url = os.environ.get("COPILOT_PROVIDER_BASE_URL", "").strip()
        if not provider_base_url:
            return AgentRuntimeAdapter.build_failure_result(
                "copilot-cli single-shot requires COPILOT_PROVIDER_BASE_URL.",
                warning=(
                    "COPILOT_PROVIDER_BASE_URL is not set; copilot-cli will not "
                    "fall back to CONNECT_AGENT_URL or OPENAI_BASE_URL."
                ),
                backend_used="copilot-cli",
            )
        provider_api_key = os.environ.get("COPILOT_PROVIDER_API_KEY", "").strip()
        effective_model = model
        if effective_model is None:
            effective_model = os.environ.get("COPILOT_MODEL", "").strip() or None
        return run_single_shot(
            prompt,
            context=context,
            system_prompt=system_prompt or _SINGLE_SHOT_SYSTEM,
            model=effective_model,
            timeout=timeout,
            max_tokens=max_tokens,
            default_system=_SINGLE_SHOT_SYSTEM,
            backend_used="copilot-cli",
            disallowed_tools=disallowed_tools,
            base_url=provider_base_url,
            api_key=provider_api_key,
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
        """Run a task via the standalone ``copilot`` CLI subprocess.

        The CLI manages its own reasoning + tool loop.  We capture stdout
        and parse the final answer from the last non-empty output block.
        """
        unsupported = self.validate_agentic_request(
            tools=tools,
            mcp_servers=mcp_servers,
            allowed_tools=allowed_tools,
            cwd=cwd,
            continuation=continuation,
        )
        if unsupported:
            return unsupported

        cli = _find_copilot_cli()
        if not cli:
            return AgenticResult(
                success=False,
                summary="copilot-cli: 'copilot' or 'copilot-cli' not found in PATH",
                backend_used="copilot-cli",
            )

        # Build the subprocess env.  Two responsibilities live here:
        #
        # 1. Model resolution.  The copilot CLI reads ``COPILOT_MODEL``
        #    directly.  We let the user set either name:
        #      * ``COPILOT_MODEL`` — explicit, takes precedence.
        #      * ``AGENT_MODEL``   — generic runtime-wide model name;
        #        propagated to ``COPILOT_MODEL`` only when the
        #        explicit one is unset, so a single ``AGENT_MODEL``
        #        in config/.env drives every backend (claude-code,
        #        connect-agent, copilot-cli, codex-cli) without the
        #        user having to maintain a parallel ``COPILOT_MODEL``.
        #    The propagation is local to the subprocess env so other
        #    adapters in the same process are not affected.
        #
        # 2. Host-credential isolation.  ``os.environ`` is inherited
        #    by the subprocess; that includes any ``GH_TOKEN`` or
        #    ``GITHUB_TOKEN`` the developer's shell may have picked
        #    up from ``gh auth login`` or a CI helper.  Per the
        #    project policy, **all** tokens must come from
        #    config/.env (or an agent-level .env) — never from the
        #    host machine.  We therefore drop those two well-known
        #    host-auth keys from the subprocess env.  Tokens that
        #    *are* meant to flow in (e.g. the COPILOT_PROVIDER_API_KEY
        #    the user set in config/.env) are not in the strip
        #    list, so they pass through unchanged.
        #
        #    We also set ``GH_CONFIG_DIR`` to an isolated empty
        #    directory for the subprocess, mirroring the hardening
        #    the claude-code adapter applies (see
        #    ``_init_isolated_gh_config`` in
        #    ``framework/runtime/claude_code/__init__.py``).  This
        #    prevents the deprecated ``gh copilot`` path from
        #    falling back to ``~/.config/gh/hosts.yml`` and reading
        #    the host's GitHub auth.  We initialise the directory
        #    lazily so each subprocess gets a fresh, empty GH config.
        env = {**os.environ}
        # Host-credential isolation: the ``gh`` fallback was removed
        # (see ``_find_copilot_cli``) so there is no path through
        # this adapter that could consult ``~/.config/gh/hosts.yml``
        # or any other host-side GitHub auth store.  We still drop
        # the two well-known host-shell keys as defence in depth, in
        # case a developer has set them in their shell (e.g. via
        # ``gh auth login`` or a CI helper); tokens that the user
        # *did* set in ``config/.env`` are not in the strip list and
        # pass through unchanged.
        for host_only_key in ("GH_TOKEN", "GITHUB_TOKEN"):
            env.pop(host_only_key, None)
        if "COPILOT_MODEL" not in env and "AGENT_MODEL" in env:
            env["COPILOT_MODEL"] = env["AGENT_MODEL"]

        full_prompt = task
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{task}"

        try:
            with cli_prompt_argument(full_prompt, backend="copilot-cli") as prompt_arg:
                # Build the command. ``cli`` is always the standalone
                # ``copilot`` binary at this point (the ``gh`` fallback was
                # removed — see ``_find_copilot_cli``). The standalone CLI's
                # bare invocation starts an interactive TUI, even when stdin
                # is populated. For containerized Constellation work we need
                # deterministic, non-interactive execution that can complete
                # without a permission prompt.
                cmd = [
                    cli,
                    "--prompt",
                    prompt_arg,
                    "--silent",
                    "--no-color",
                    "--no-auto-update",
                    "--output-format",
                    "text",
                    "--allow-all-tools",
                    "--allow-all-paths",
                    "--no-ask-user",
                    "--secret-env-vars=COPILOT_PROVIDER_API_KEY,COPILOT_GITHUB_TOKEN,GH_TOKEN,GITHUB_TOKEN",
                ]
                effective_model = env.get("COPILOT_MODEL")
                if effective_model:
                    cmd.extend(["--model", effective_model])

                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    timeout=timeout,
                    env=env,
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
        return self.agentic_capabilities().mcp_servers

    def agentic_capabilities(self) -> AgenticCapabilities:
        return AgenticCapabilities(
            backend="copilot-cli",
            agentic=True,
            constellation_tools=False,
            mcp_servers=False,
            cwd=True,
            allowed_tools=False,
            continuation=False,
            plugin_hooks=False,
        )
