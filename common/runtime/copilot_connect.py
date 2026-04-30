"""OpenAI-compatible runtime backend used for Copilot Connect and local integration tests."""

from __future__ import annotations

import json
import os
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common.env_utils import env_flag, resolve_openai_base_url
from common.runtime.adapter import AgenticResult, AgentRuntimeAdapter

DEFAULT_MODEL = "gpt-5-mini"


def _mock_response(prompt: str, model: str) -> str:
    preview = prompt.strip()
    if len(preview) > 240:
        preview = preview[:240] + "\n...[truncated]..."
    return f"MOCK_LLM_RESPONSE\nmodel={model}\nprompt={preview}"


class CopilotConnectAdapter(AgentRuntimeAdapter):
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
        effective_model = self.resolve_model(
            model,
            os.environ.get("AGENT_MODEL"),
            os.environ.get("OPENAI_MODEL"),
            fallback=DEFAULT_MODEL,
        )
        effective_system = self.build_prompt(
            "",
            system_prompt=system_prompt or self.DEFAULT_SYSTEM,
            context=context,
        ).strip()
        endpoint = f"{resolve_openai_base_url()}/chat/completions"
        payload = {
            "model": effective_model,
            "messages": [
                {"role": "system", "content": effective_system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        headers = {"Content-Type": "application/json; charset=utf-8"}
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        request = Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urlopen(request, timeout=timeout) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            warning = f"copilot-connect HTTP {exc.code}: {body[:300]}"
            if env_flag("ALLOW_MOCK_FALLBACK", default=False):
                raw = _mock_response(prompt, effective_model)
                return self.build_result(raw, warnings=[warning, "Fell back to mock response."], backend_used="copilot-connect")
            return self.build_failure_result(
                f"Copilot Connect request failed with HTTP {exc.code}.",
                warning=warning,
                backend_used="copilot-connect",
            )
        except URLError as exc:
            warning = f"copilot-connect network error: {exc.reason}"
            if env_flag("ALLOW_MOCK_FALLBACK", default=False):
                raw = _mock_response(prompt, effective_model)
                return self.build_result(raw, warnings=[warning, "Fell back to mock response."], backend_used="copilot-connect")
            return self.build_failure_result(
                "Copilot Connect request failed because the endpoint is unreachable.",
                warning=warning,
                backend_used="copilot-connect",
            )

        choices = response_payload.get("choices") or []
        if not choices:
            return self.build_failure_result(
                "Copilot Connect returned no choices.",
                warning=f"Unexpected payload: {json.dumps(response_payload, ensure_ascii=False)[:300]}",
                backend_used="copilot-connect",
            )

        content = (choices[0].get("message") or {}).get("content", "")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        raw = str(content or "").strip()
        return self.build_result(raw, backend_used="copilot-connect")

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
        """Multi-turn agentic execution via OpenAI function_calling.

        Used when MCP-native backends (claude-code, copilot-cli) are not
        available — falls back to a tool-enabled multi-turn conversation loop.
        """
        effective_model = self.resolve_model(
            os.environ.get("AGENT_MODEL"),
            os.environ.get("OPENAI_MODEL"),
            fallback=DEFAULT_MODEL,
        )
        endpoint = f"{resolve_openai_base_url()}/chat/completions"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        functions: list[dict] = []
        if tools:
            try:
                from common.tools.native_adapter import get_function_definitions
                functions = get_function_definitions()
            except ImportError:
                pass

        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        else:
            messages.append({"role": "system", "content": self.DEFAULT_SYSTEM})
        messages.append({"role": "user", "content": task})

        all_tool_calls: list[dict] = []
        turns_used = 0
        final_content = ""

        for turn in range(max_turns):
            turns_used = turn + 1
            payload: dict = {
                "model": effective_model,
                "messages": messages,
                "stream": False,
                "temperature": 0,
                "max_tokens": 4096,
            }
            if functions:
                payload["tools"] = functions
                payload["tool_choice"] = "auto"

            request = Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )

            try:
                with urlopen(request, timeout=min(timeout, 120)) as resp:
                    response_payload = json.loads(resp.read().decode("utf-8"))
            except (HTTPError, URLError) as exc:
                return AgenticResult(
                    success=False,
                    summary=f"copilot-connect agentic request failed: {exc}",
                    turns_used=turns_used,
                    backend_used="copilot-connect",
                )

            choices = response_payload.get("choices") or []
            if not choices:
                break

            choice = choices[0]
            finish_reason = choice.get("finish_reason", "stop")
            message = choice.get("message") or {}
            content = message.get("content") or ""
            tool_calls_raw = message.get("tool_calls") or []

            if finish_reason == "stop" or not tool_calls_raw:
                final_content = str(content).strip()
                if on_progress:
                    on_progress(final_content[:300])
                break

            # Execute tool calls and feed results back
            messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls_raw})

            for tc in tool_calls_raw:
                fn = tc.get("function") or {}
                fn_name = fn.get("name", "")
                fn_args_raw = fn.get("arguments", "{}")
                try:
                    fn_args = json.loads(fn_args_raw)
                except json.JSONDecodeError:
                    fn_args = {}

                tool_result_text = f"Tool '{fn_name}' not found."
                try:
                    from common.tools.native_adapter import dispatch_function_call
                    tool_result_text = dispatch_function_call(fn_name, fn_args)
                except Exception as exc:  # noqa: BLE001
                    tool_result_text = f"Error executing '{fn_name}': {exc}"

                all_tool_calls.append({"name": fn_name, "args": fn_args, "result": tool_result_text[:500]})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": tool_result_text,
                })

            if on_progress:
                on_progress(f"[turn {turns_used}] executed {len(tool_calls_raw)} tool call(s)")

        return AgenticResult(
            success=True,
            summary=final_content or "(no final response)",
            tool_calls=all_tool_calls,
            raw_output=final_content,
            turns_used=turns_used,
            backend_used="copilot-connect",
        )


from common.runtime.provider_registry import register_runtime  # noqa: E402

register_runtime("copilot-connect", CopilotConnectAdapter)
