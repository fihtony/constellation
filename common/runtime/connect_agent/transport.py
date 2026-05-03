"""Shared Copilot Connect transport for connect-agent runtime variants."""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common.env_utils import resolve_openai_base_url
from common.runtime.adapter import AgentRuntimeAdapter

DEFAULT_MODEL = "gpt-5-mini"


def extract_text(response_payload: dict) -> str:
    choices = response_payload.get("choices") or []
    if not choices:
        return ""
    content = (choices[0].get("message") or {}).get("content", "")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content or "").strip()


def call_chat_completion(
    messages: list[dict],
    *,
    model: str,
    timeout: int = 120,
    max_tokens: int = 4096,
    tools: list[dict] | None = None,
    temperature: float = 0,
) -> dict:
    endpoint = f"{resolve_openai_base_url()}/chat/completions"
    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

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
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def run_single_shot(
    prompt: str,
    *,
    context: dict | None = None,
    system_prompt: str | None = None,
    model: str | None = None,
    timeout: int = 120,
    max_tokens: int = 4096,
    default_system: str,
    backend_used: str,
) -> dict:
    effective_model = AgentRuntimeAdapter.resolve_model(
        model,
        os.environ.get("AGENT_MODEL"),
        os.environ.get("OPENAI_MODEL"),
        fallback=DEFAULT_MODEL,
    )
    effective_system = AgentRuntimeAdapter.build_prompt(
        "",
        system_prompt=system_prompt or default_system,
        context=context,
    ).strip()
    messages = [
        {"role": "system", "content": effective_system},
        {"role": "user", "content": prompt},
    ]

    try:
        response_payload = call_chat_completion(
            messages,
            model=effective_model,
            timeout=timeout,
            max_tokens=max_tokens,
        )
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        warning = f"{backend_used} HTTP {exc.code}: {body[:300]}"
        return AgentRuntimeAdapter.build_failure_result(
            f"{backend_used} request failed with HTTP {exc.code}.",
            warning=warning,
            backend_used=backend_used,
        )
    except URLError as exc:
        warning = f"{backend_used} network error: {exc.reason}"
        return AgentRuntimeAdapter.build_failure_result(
            f"{backend_used} request failed because the endpoint is unreachable.",
            warning=warning,
            backend_used=backend_used,
        )

    choices = response_payload.get("choices") or []
    if not choices:
        return AgentRuntimeAdapter.build_failure_result(
            f"{backend_used} returned no choices.",
            warning=f"Unexpected payload: {json.dumps(response_payload, ensure_ascii=False)[:300]}",
            backend_used=backend_used,
        )

    raw = extract_text(response_payload)
    return AgentRuntimeAdapter.build_result(raw, backend_used=backend_used)