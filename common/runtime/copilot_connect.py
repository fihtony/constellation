"""OpenAI-compatible runtime backend used for Copilot Connect and local integration tests."""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common.env_utils import env_flag
from common.runtime.adapter import AgentRuntimeAdapter

DEFAULT_BASE_URL = "http://localhost:1288/v1"
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
        endpoint = f"{os.environ.get('OPENAI_BASE_URL', DEFAULT_BASE_URL).rstrip('/')}/chat/completions"
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
            if env_flag("ALLOW_MOCK_FALLBACK", default=True):
                raw = _mock_response(prompt, effective_model)
                return self.build_result(raw, warnings=[warning, "Fell back to mock response."], backend_used="copilot-connect")
            return self.build_failure_result(
                f"Copilot Connect request failed with HTTP {exc.code}.",
                warning=warning,
                backend_used="copilot-connect",
            )
        except URLError as exc:
            warning = f"copilot-connect network error: {exc.reason}"
            if env_flag("ALLOW_MOCK_FALLBACK", default=True):
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