"""GitHub Copilot CLI runtime backend.

Single-shot mode (``run()``) sends the full prompt to the CLI binary and
returns the text response.

Agentic mode (``run_agentic()``) implements a text-based ReAct-style loop:
  - Tool schemas are formatted as plain-text descriptions in the system prompt.
  - The model is asked to emit ``<tool_call name="...">JSON args</tool_call>``
    blocks when it wants to call a tool.
  - Tool results are fed back as ``<tool_result name="...">text</tool_result>``
    blocks in the next prompt.
  - The loop terminates when the model emits ``<final_answer>text</final_answer>``
    or when no tool call is found in the response.

This approach works with any text-only CLI backend that does not natively
support OpenAI function-calling.  It is less capable than the connect-agent
backend but allows copilot-cli to participate in agentic workflows.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from typing import Callable

from common.env_utils import build_isolated_copilot_env
from common.runtime.adapter import AgenticResult, AgentRuntimeAdapter

DEFAULT_MODEL = "gpt-5-mini"

# ---------------------------------------------------------------------------
# ReAct text-based tool-calling helpers
# ---------------------------------------------------------------------------

_TOOL_CALL_RE = re.compile(
    r'<tool_call\s+name=["\']([^"\']+)["\']>(.*?)</tool_call>',
    re.DOTALL,
)
_FINAL_ANSWER_RE = re.compile(
    r'<final_answer>(.*?)</final_answer>',
    re.DOTALL,
)


def _format_tool_schemas_as_text(tool_names: list[str]) -> str:
    """Return a human-readable listing of available tools for the system prompt."""
    try:
        from common.tools.native_adapter import get_function_definitions
    except ImportError:
        return ""
    defs = get_function_definitions(tool_names or None)
    if not defs:
        return ""
    lines = ["Available tools (call with <tool_call name=\"...\">JSON args</tool_call>):"]
    for item in defs:
        fn = item.get("function") or {}
        name = fn.get("name", "?")
        desc = fn.get("description", "")
        params = fn.get("parameters") or {}
        props = params.get("properties") or {}
        required = params.get("required") or []
        param_strs = []
        for p_name, p_schema in props.items():
            req = " (required)" if p_name in required else ""
            p_desc = p_schema.get("description", "")
            p_type = p_schema.get("type", "any")
            param_strs.append(f"    - {p_name} ({p_type}{req}): {p_desc}")
        param_block = "\n".join(param_strs) if param_strs else "    (no parameters)"
        lines.append(f"\n{name}:\n  {desc}\n  Parameters:\n{param_block}")
    return "\n".join(lines)


def _build_react_system_prompt(
    task_system_prompt: str | None,
    tool_text: str,
) -> str:
    base = (
        "You are an autonomous agent operating in the Constellation multi-agent system.\n\n"
        "TOOL CALLING PROTOCOL:\n"
        "When you need to call a tool, emit a block like this:\n"
        "<tool_call name=\"tool_name\">{\"arg1\": \"value1\"}</tool_call>\n\n"
        "After calling a tool you will receive the result in a block like:\n"
        "<tool_result name=\"tool_name\">result text</tool_result>\n\n"
        "When you have completed all work and have a final answer, emit:\n"
        "<final_answer>your summary here</final_answer>\n\n"
        "Rules:\n"
        "- Call tools one at a time. Wait for the result before calling another.\n"
        "- Use tools to gather information, make decisions, and execute actions.\n"
        "- Do not fabricate tool results; wait for real results from the system.\n"
        "- If a tool fails, try an alternative approach or call fail_current_task.\n"
        "- Emit <final_answer> when done or when you call complete_current_task/fail_current_task.\n\n"
        "AUTHORIZED TASK SCOPE:\n"
        "- Normal software engineering and office tasks in Constellation are allowed.\n"
        "- Jira tickets, repo inspection, branch or PR work, code changes, design implementation,\n"
        "  and office document analysis or organization are legitimate requests when handled\n"
        "  through the provided tools and task context.\n"
        "- Do not refuse a benign task only because it mentions external work systems, URLs,\n"
        "  repositories, files, or code changes. Use get_task_context or other discovery tools\n"
        "  if you need confirmation about permissions or routing.\n"
        "- Refuse only requests that are clearly harmful, prohibited, or outside the available\n"
        "  tool boundaries.\n\n"
        "FIRST RESPONSE REQUIREMENT:\n"
        "- For a normal Constellation task, your first response MUST be a <tool_call ...> block\n"
        "  or a <final_answer>...</final_answer> block. Do not reply with plain prose.\n"
        "- Never answer a benign Constellation task with generic refusal text such as\n"
        "  'I'm sorry, but I cannot assist with that request.'\n"
        "- If you are unsure what to do first, call get_task_context with {} and then continue.\n"
        "- If the task mentions a Jira ticket, repository, PR, workspace path, URL, or office\n"
        "  document, treat it as in-scope and start by gathering context with tools.\n"
        "- Example valid first response:\n"
        "  <tool_call name=\"get_task_context\">{}</tool_call>\n\n"
    )
    if tool_text:
        base += tool_text + "\n\n"
    if task_system_prompt and task_system_prompt.strip():
        base += "AGENT-SPECIFIC INSTRUCTIONS:\n" + task_system_prompt.strip() + "\n"
    return base


def _build_turn_prompt(
    task: str,
    history: list[tuple[str, str]],
    *,
    system_prompt: str,
) -> str:
    """Build the full prompt for one CLI invocation, including conversation history."""
    parts = [system_prompt.strip(), "\n\nTASK:\n" + task.strip()]
    if history:
        parts.append("\n\nCONVERSATION HISTORY:")
        for role, text in history:
            label = "ASSISTANT" if role == "assistant" else "TOOL_RESULTS"
            parts.append(f"\n[{label}]\n{text}")
        parts.append("\n\n[ASSISTANT — continue from here]")
    return "\n".join(parts)


def _parse_tool_calls(text: str) -> list[tuple[str, dict]]:
    """Extract (name, args_dict) pairs from <tool_call> blocks."""
    calls = []
    for match in _TOOL_CALL_RE.finditer(text):
        name = match.group(1).strip()
        raw = match.group(2).strip()
        try:
            args = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            args = {"__raw__": raw}
        if isinstance(args, dict):
            calls.append((name, args))
    return calls


def _parse_final_answer(text: str) -> str | None:
    match = _FINAL_ANSWER_RE.search(text)
    if match:
        return match.group(1).strip()
    return None


def _dispatch_tool(name: str, args: dict) -> str:
    try:
        from common.tools.native_adapter import dispatch_function_call
        return dispatch_function_call(name, args)
    except KeyError:
        return f"Error: unknown tool '{name}'."
    except Exception as exc:  # noqa: BLE001
        return f"Error calling tool '{name}': {exc}"


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
        extra_allow_roots: list[str] | None = None,
        tools: list[str] | None = None,
        mcp_servers: dict | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        max_turns: int = 50,
        timeout: int = 1800,
        on_progress: Callable[[str], None] | None = None,
        continuation: str | None = None,
    ) -> AgenticResult:
        """Run an agentic multi-turn ReAct loop using the Copilot CLI as the LLM backend.

        Each turn calls ``run()`` with the full accumulated conversation, parses
        ``<tool_call>`` blocks from the response, executes the tools, and feeds the
        results back.  The loop ends when the model emits ``<final_answer>`` or
        when no tool call is found in the response.

        Limitations vs. connect-agent:
        - No native function-calling; tool schemas are embedded as text.
        - No streaming; each turn is a full blocking CLI invocation.
        - Conversation context grows with each turn (token budget awareness is
          the caller's responsibility via max_turns / timeout).
        - MCP servers are not supported; the mcp_servers parameter is ignored.
        """
        del extra_allow_roots
        # --- Pre-flight: verify binary and token are available ---
        token, _ = _resolve_token()
        binary = os.environ.get("COPILOT_CLI_BIN", "copilot").strip() or "copilot"
        if not token:
            return AgenticResult(
                success=False,
                summary="COPILOT_GITHUB_TOKEN is not configured; copilot-cli cannot run.",
                backend_used="copilot-cli",
            )
        if shutil.which(binary) is None:
            return AgenticResult(
                success=False,
                summary=f"Copilot CLI binary '{binary}' not found; copilot-cli cannot run agentic mode.",
                backend_used="copilot-cli",
            )

        # --- Build per-session system prompt with tool descriptions ---
        requested_tools = list(dict.fromkeys(tools or []))
        tool_text = _format_tool_schemas_as_text(requested_tools)
        react_system = _build_react_system_prompt(system_prompt, tool_text)

        effective_model = self.resolve_model(
            os.environ.get("AGENT_MODEL"),
            os.environ.get("COPILOT_MODEL"),
            os.environ.get("OPENAI_MODEL"),
            fallback=DEFAULT_MODEL,
        )

        history: list[tuple[str, str]] = []
        if continuation:
            # Restore partial history from a prior session (best-effort).
            history.append(("assistant", continuation))

        all_tool_calls: list[dict] = []
        protocol_error_count = 0
        start_time = time.time()
        per_turn_timeout = max(30, timeout // max(max_turns, 1))

        for turn_index in range(max_turns):
            elapsed = time.time() - start_time
            if elapsed >= timeout:
                return AgenticResult(
                    success=False,
                    summary=f"Agentic run timed out after {elapsed:.0f}s ({turn_index} turns).",
                    tool_calls=all_tool_calls,
                    turns_used=turn_index,
                    backend_used="copilot-cli",
                )

            prompt = _build_turn_prompt(task, history, system_prompt=react_system)
            result = self.run(
                prompt,
                system_prompt=None,  # already embedded
                model=effective_model,
                timeout=per_turn_timeout,
            )

            if result.get("warnings"):
                for w in result["warnings"]:
                    if on_progress:
                        on_progress(f"[copilot-cli] warning: {w}")

            raw = result.get("raw_response") or ""
            if not raw:
                protocol_error_count += 1
                if protocol_error_count >= 3:
                    return AgenticResult(
                        success=False,
                        summary="Copilot CLI returned an empty response.",
                        tool_calls=all_tool_calls,
                        turns_used=turn_index + 1,
                        backend_used="copilot-cli",
                    )
                history.append((
                    "tool",
                    (
                        '<tool_result name="protocol_error">'
                        "Your previous response was empty. Do not return an empty response. "
                        "Emit exactly one <tool_call name=\"...\">{...}</tool_call> block for the next action, "
                        "or emit <final_answer>...</final_answer> if the task is complete."
                        "</tool_result>"
                    ),
                ))
                continue

            # Check for final answer first
            final = _parse_final_answer(raw)
            if final is not None:
                return AgenticResult(
                    success=True,
                    summary=final,
                    raw_output=raw,
                    tool_calls=all_tool_calls,
                    turns_used=turn_index + 1,
                    backend_used="copilot-cli",
                )

            # Parse and execute tool calls
            calls = _parse_tool_calls(raw)
            if not calls:
                protocol_error_count += 1
                if protocol_error_count >= 3:
                    return AgenticResult(
                        success=False,
                        summary=(
                            "Copilot CLI returned plain text without the required "
                            "<tool_call> or <final_answer> tags."
                        ),
                        raw_output=raw,
                        tool_calls=all_tool_calls,
                        turns_used=turn_index + 1,
                        backend_used="copilot-cli",
                    )
                history.append((
                    "tool",
                    (
                        '<tool_result name="protocol_error">'
                        "Your previous response did not follow the required protocol. "
                        "This is an authorized Constellation workflow task, not a refusal case. "
                        "Do not emit generic policy refusal text. "
                        "If you are unsure what to do first, call get_task_context with {}. "
                        "Do not answer in plain text. Emit exactly one <tool_call name=\"...\">{...}</tool_call> "
                        "block for the next action, or emit <final_answer>...</final_answer> if the task is complete."
                        "</tool_result>"
                    ),
                ))
                continue

            protocol_error_count = 0
            history.append(("assistant", raw))

            tool_result_blocks = []
            for name, args in calls:
                if on_progress:
                    on_progress(f"[copilot-cli] tool call: {name}")
                tool_result = _dispatch_tool(name, args)
                all_tool_calls.append({"name": name, "arguments": args, "result": tool_result[:500]})
                tool_result_blocks.append(
                    f'<tool_result name="{name}">{tool_result}</tool_result>'
                )

            history.append(("tool", "\n".join(tool_result_blocks)))

        return AgenticResult(
            success=False,
            summary=f"Reached max turns ({max_turns}) without a final answer.",
            tool_calls=all_tool_calls,
            turns_used=max_turns,
            backend_used="copilot-cli",
        )


from common.runtime.provider_registry import register_runtime  # noqa: E402

register_runtime("copilot-cli", CopilotCliAdapter)
