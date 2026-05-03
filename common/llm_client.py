"""OpenAI-compatible client shared by the Compass agent and worker containers.

LLM backend priority (first available wins):
  1. MOCK_LLM=1 → deterministic mock (testing only)
  2. USE_COPILOT_CLI=1  OR  COPILOT_GITHUB_TOKEN set + copilot binary present
     → GitHub Copilot CLI  (copilot --model MODEL -sp "PROMPT")
  3. OPENAI_BASE_URL → OpenAI-compatible REST API
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common.env_utils import build_isolated_copilot_env, env_flag, resolve_openai_base_url

DEFAULT_MODEL = "gpt-5-mini"


def _base_url():
    return resolve_openai_base_url()


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


# ---------------------------------------------------------------------------
# Copilot CLI backend
# ---------------------------------------------------------------------------

def _copilot_available() -> bool:
    """Return True when Copilot CLI is installed and a token is configured."""
    if not os.environ.get("COPILOT_GITHUB_TOKEN", "").strip():
        return False
    return shutil.which("copilot") is not None


def _copilot_generate(prompt: str, actor_label: str, system_prompt: str | None = None) -> str:
    """Call GitHub Copilot CLI non-interactively.

    System prompt is prepended to the user prompt (Copilot CLI has no system role).
    """
    token = os.environ.get("COPILOT_GITHUB_TOKEN", "")
    model = os.environ.get("COPILOT_MODEL", _model())
    full_prompt = prompt
    if system_prompt:
        full_prompt = f"{system_prompt}\n\n{prompt}"
    cmd = ["copilot", "--model", model, "-sp", full_prompt]
    env = build_isolated_copilot_env(token)
    print(f"[llm] {actor_label} invoking: copilot CLI model={model}")
    print(f"[llm] {actor_label} prompt:")
    print(_preview_text(full_prompt))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            print(f"[llm] {actor_label} copilot CLI exit {result.returncode}: {err[:300]}")
            # Fall through to OpenAI fallback
            return ""
        content = result.stdout.strip()
        print(f"[llm] {actor_label} copilot CLI response (via copilot CLI):")
        print(_preview_text(content))
        return content
    except subprocess.TimeoutExpired:
        print(f"[llm] {actor_label} copilot CLI timed out after 180s")
        return ""
    except FileNotFoundError:
        print(f"[llm] {actor_label} copilot binary not found")
        return ""


def generate_text(prompt, actor_label, *, system_prompt=None, temperature=0):
    """Generate text from an LLM.

    Backend priority:
      1. MOCK_LLM=1  → mock
      2. Copilot CLI (when COPILOT_GITHUB_TOKEN is set and copilot binary exists)
      3. OpenAI-compatible REST API
    """
    if env_flag("MOCK_LLM", default=False):
        response = _mock_response(prompt)
        print(f"[llm] {actor_label} mock response generated")
        return response

    # Try Copilot CLI first if available
    if _copilot_available() and not env_flag("DISABLE_COPILOT_CLI", default=False):
        content = _copilot_generate(prompt, actor_label, system_prompt=system_prompt)
        if content:
            return content
        print(f"[llm] {actor_label} copilot CLI returned empty — falling back to OpenAI API")

    # OpenAI-compatible REST API
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
        raise RuntimeError(
            f"OpenAI-compatible API failed inside the {actor_label.lower()} container. "
            f"HTTP {error.code}: {body}"
        ) from error
    except URLError as error:
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