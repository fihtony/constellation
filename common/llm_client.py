"""OpenAI-compatible client shared by the Compass agent and worker containers."""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common.env_utils import env_flag

DEFAULT_BASE_URL = "http://localhost:1288/v1"
DEFAULT_MODEL = "gpt-5-mini"


def _base_url():
    return os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _model():
    return os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)


def _preview_text(text, limit=1000):
    text = str(text).strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n...[truncated]..."


def _mock_response(prompt):
    preview = _preview_text(prompt, limit=240)
    return (
        "MOCK_LLM_RESPONSE\n"
        f"model={_model()}\n"
        f"prompt={preview}"
    )


def generate_text(prompt, actor_label, *, system_prompt=None, temperature=0):
    """Generate text via an OpenAI-compatible chat completions API.

    Set MOCK_LLM=1 for deterministic offline testing.
    """
    if env_flag("MOCK_LLM", default=False):
        response = _mock_response(prompt)
        print(f"[llm] {actor_label} mock response generated")
        return response

    endpoint = f"{_base_url()}/chat/completions"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": _model(),
        "messages": messages,
        "stream": False,
        "temperature": temperature,
    }

    headers = {"Content-Type": "application/json; charset=utf-8"}
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    print(f"[llm] {actor_label} invoking: POST {endpoint} model={_model()}")
    print(f"[llm] {actor_label} prompt:")
    print(_preview_text(prompt))

    try:
        with urlopen(request, timeout=120) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        if env_flag("ALLOW_MOCK_FALLBACK", default=True):
            print(f"[llm] {actor_label} falling back to mock after HTTP {error.code}: {body}")
            return _mock_response(prompt)
        raise RuntimeError(
            f"OpenAI-compatible API failed inside the {actor_label.lower()} container. "
            f"HTTP {error.code}: {body}"
        ) from error
    except URLError as error:
        if env_flag("ALLOW_MOCK_FALLBACK", default=True):
            print(f"[llm] {actor_label} falling back to mock after network error: {error.reason}")
            return _mock_response(prompt)
        raise RuntimeError(
            f"OpenAI-compatible API failed inside the {actor_label.lower()} container. "
            f"{error.reason}"
        ) from error

    choices = response_payload.get("choices", [])
    if not choices:
        raise RuntimeError(
            f"OpenAI-compatible API returned no choices for {actor_label}. Payload: {response_payload}"
        )

    message = choices[0].get("message", {})
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    content = str(content).strip()

    print(f"[llm] {actor_label} exit code: 0")
    if content:
        print(f"[llm] {actor_label} response:")
        print(content)

    return content