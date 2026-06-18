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

DEFAULT_MODEL = "gpt-5-mini"


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
    base_url: str | None = None,
    api_key: str | None = None,
) -> dict:
    """Send a chat-completion request and return the raw response dict."""
    resolved_base_url = (base_url or resolve_openai_base_url()).strip().rstrip("/")
    endpoint = f"{resolved_base_url}/chat/completions"
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
    resolved_api_key = (
        api_key.strip()
        if api_key is not None
        else os.environ.get("OPENAI_API_KEY", "").strip()
    )
    if resolved_api_key:
        headers["Authorization"] = f"Bearer {resolved_api_key}"

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


def _classify_url_error(
    exc: URLError, backend_used: str
) -> tuple[str, str]:
    """Return a (summary, warning) pair that names the actual transport
    failure rather than the catch-all "endpoint is unreachable".

    Why this exists: the previous handler returned the same generic
    message for *every* ``URLError`` sub-type.  In task-63432d83fc65 the
    real cause was an LLM request that ran past its 90s timeout (the
    earlier ``FutureTimeoutError`` was wrapped as a ``URLError`` at
    :func:`call_chat_completion`); the warning string held the truth
    but the summary still read "endpoint is unreachable", so the
    orchestrator and the user both misread the failure as a network
    outage.  Naming the sub-type directly is enough to disambiguate
    timeout / DNS / TLS / refused / generic without introducing any
    retry or fallback behaviour (per project policy, LLM transport
    failures must still fail the task).

    The returned ``summary`` is what callers see in
    ``result["summary"]`` and ``result["raw_response"]``; the
    ``warning`` keeps the raw reason for log forensics.  The mapping
    is deliberately conservative — a new reason falls through to the
    generic message rather than guessing.
    """
    raw_reason = str(getattr(exc, "reason", "") or "")
    lowered = raw_reason.lower()
    warning = f"{backend_used} network error: {raw_reason}"
    if "timed out" in lowered or "timeout" in lowered:
        return (
            f"{backend_used} request timed out.",
            warning,
        )
    if "name or service not known" in lowered or "nodename nor servname" in lowered:
        return (
            f"{backend_used} DNS resolution failed.",
            warning,
        )
    if "connection refused" in lowered:
        return (
            f"{backend_used} endpoint refused connection.",
            warning,
        )
    if (
        "ssl" in lowered
        or "certificate" in lowered
        or "handshake" in lowered
        or "cert_verify" in lowered
    ):
        return (
            f"{backend_used} TLS handshake failed.",
            warning,
        )
    if "connection reset" in lowered or "broken pipe" in lowered:
        return (
            f"{backend_used} connection was reset mid-request.",
            warning,
        )
    return (
        f"{backend_used} request failed because the endpoint is unreachable.",
        warning,
    )


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
    cwd: str | None = None,
    disallowed_tools: list[str] | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> dict:
    """Single-shot prompt → response via chat-completion.

    When *plugin_manager* is provided, fires ``before_llm_call`` before the
    request and ``after_llm_response`` after receiving the response.

    *cwd* is accepted for API compatibility but has no effect on remote calls.

    *disallowed_tools* is a structural no-op for the remote API path:
    the LLM is not given a tool surface to begin with.  We accept the
    argument so every backend shares one contract — callers can pass
    it unconditionally without branching on the backend name.
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
            base_url=base_url,
            api_key=api_key,
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
        summary, warning = _classify_url_error(exc, backend_used)
        return AgentRuntimeAdapter.build_failure_result(
            summary,
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
