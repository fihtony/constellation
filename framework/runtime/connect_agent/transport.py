"""Copilot Connect / OpenAI-compatible transport layer.

Handles HTTP chat-completion calls (single-shot and multi-turn with tool calling).
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from framework.env_utils import resolve_openai_base_url
from framework.runtime.adapter import AgentRuntimeAdapter

DEFAULT_MODEL = "gpt-5.4-mini"


def _debug_logging_enabled() -> bool:
    return os.environ.get("CONNECT_AGENT_DEBUG_LOG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _debug_log(event: str, **fields) -> None:
    if not _debug_logging_enabled():
        return
    payload = {
        "ts": int(time.time() * 1000),
        "event": event,
        **fields,
    }
    print(
        f"[connect-agent] {json.dumps(payload, ensure_ascii=False, sort_keys=True)}",
        file=sys.stderr,
        flush=True,
    )


def _read_http_error_body(exc: HTTPError) -> str:
    cached = getattr(exc, "_cached_body", None)
    if cached is not None:
        return cached
    body = exc.read().decode("utf-8", errors="replace")
    setattr(exc, "_cached_body", body)
    return body


def _should_retry_capacity_error(exc: HTTPError, body: str) -> bool:
    if exc.code != 503:
        return False
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False
    error = payload.get("error") or {}
    return error.get("code") == "no_choices" or error.get("type") == "upstream_capacity_error"


def _retry_delay_seconds(exc: HTTPError, attempt: int) -> float:
    header_val = exc.headers.get("Retry-After") if exc.headers else None
    if header_val:
        try:
            return max(0.0, float(header_val))
        except ValueError:
            pass
    return float(2 ** attempt)


def _parse_error_summary(body: str) -> tuple[str | None, str | None]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None, None
    error = payload.get("error") or {}
    return error.get("type"), error.get("code")


def _perform_chat_request(request: Request, timeout: int) -> dict:
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def extract_text(response_payload: dict) -> str:
    """Extract assistant text from a chat-completion response."""
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
    """Send a chat-completion request and return the raw response dict."""
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
    max_retries = max(0, int(os.environ.get("CONNECT_AGENT_MAX_RETRIES", "2")))
    last_http_error: HTTPError | None = None
    for attempt in range(max_retries + 1):
        request_id = f"{int(time.time() * 1000)}-{attempt + 1}"
        _debug_log(
            "request.start",
            requestId=request_id,
            endpoint=endpoint,
            model=model,
            attempt=attempt + 1,
            timeoutSeconds=timeout,
            messageCount=len(messages),
            toolCount=len(tools or []),
        )
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(_perform_chat_request, request, timeout)
        try:
            response_payload = future.result(timeout=max(1, timeout + 2))
        except FutureTimeoutError as exc:
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            _debug_log(
                "request.timeout",
                requestId=request_id,
                endpoint=endpoint,
                model=model,
                attempt=attempt + 1,
                timeoutSeconds=timeout,
            )
            raise URLError(f"connect-agent request timed out after {timeout}s") from exc
        except HTTPError as exc:
            executor.shutdown(wait=False, cancel_futures=True)
            body = _read_http_error_body(exc)
            error_type, error_code = _parse_error_summary(body)
            _debug_log(
                "request.http_error",
                requestId=request_id,
                endpoint=endpoint,
                model=model,
                attempt=attempt + 1,
                httpStatus=exc.code,
                errorType=error_type,
                errorCode=error_code,
            )
            if attempt < max_retries and _should_retry_capacity_error(exc, body):
                delay = _retry_delay_seconds(exc, attempt)
                _debug_log(
                    "request.retry",
                    requestId=request_id,
                    endpoint=endpoint,
                    model=model,
                    attempt=attempt + 1,
                    retryDelaySeconds=delay,
                )
                time.sleep(delay)
                last_http_error = exc
                continue
            raise
        except URLError as exc:
            executor.shutdown(wait=False, cancel_futures=True)
            _debug_log(
                "request.network_error",
                requestId=request_id,
                endpoint=endpoint,
                model=model,
                attempt=attempt + 1,
                reason=str(exc.reason),
            )
            raise
        except Exception:
            executor.shutdown(wait=False, cancel_futures=True)
            raise

        executor.shutdown(wait=False, cancel_futures=True)
        _debug_log(
            "request.success",
            requestId=request_id,
            endpoint=endpoint,
            model=model,
            attempt=attempt + 1,
            choiceCount=len(response_payload.get("choices") or []),
        )
        return response_payload

    if last_http_error is not None:
        raise last_http_error
    raise RuntimeError("connect-agent request failed without an HTTP response")


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
    plugin_manager=None,
) -> dict:
    """Single-shot prompt → response via chat-completion.

    When *plugin_manager* is provided, fires ``before_llm_call`` before the
    request and ``after_llm_response`` after receiving the response.
    """
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

    # Plugin: before_llm_call
    if plugin_manager:
        plugin_manager.fire_sync("before_llm_call", prompt, ctx={})

    try:
        response_payload = call_chat_completion(
            messages,
            model=effective_model,
            timeout=timeout,
            max_tokens=max_tokens,
        )
    except HTTPError as exc:
        body = _read_http_error_body(exc)
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

    # Plugin: after_llm_response
    if plugin_manager:
        plugin_manager.fire_sync("after_llm_response", raw, ctx={})

    return AgentRuntimeAdapter.build_result(raw, backend_used=backend_used)
