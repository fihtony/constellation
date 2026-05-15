"""Tests for connect-agent transport retry behaviour."""

from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeoutError
import io
import json
from email.message import Message
from urllib.error import HTTPError, URLError


def _http_error(code: int, body: dict, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(
        url="http://localhost:1288/v1/chat/completions",
        code=code,
        msg="error",
        hdrs=headers,
        fp=io.BytesIO(json.dumps(body).encode("utf-8")),
    )


def test_call_chat_completion_retries_no_choices(monkeypatch):
    from framework.runtime.connect_agent.transport import call_chat_completion

    calls = {"count": 0}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [{
                    "message": {"content": "ok"},
                    "finish_reason": "stop",
                }]
            }).encode("utf-8")

    def _urlopen(request, timeout=120):
        calls["count"] += 1
        if calls["count"] == 1:
            raise _http_error(503, {
                "error": {
                    "message": "Response contained no choices.",
                    "type": "upstream_capacity_error",
                    "code": "no_choices",
                }
            }, retry_after="0")
        return _Response()

    monkeypatch.setenv("CONNECT_AGENT_MAX_RETRIES", "1")
    monkeypatch.setattr("framework.runtime.connect_agent.transport.urlopen", _urlopen)
    monkeypatch.setattr("framework.runtime.connect_agent.transport.time.sleep", lambda seconds: None)

    payload = call_chat_completion(
        [{"role": "user", "content": "hello"}],
        model="gpt-5-mini",
        timeout=10,
    )

    assert calls["count"] == 2
    assert payload["choices"][0]["message"]["content"] == "ok"


def test_call_chat_completion_does_not_retry_non_capacity_http_error(monkeypatch):
    from framework.runtime.connect_agent.transport import call_chat_completion

    calls = {"count": 0}

    def _urlopen(request, timeout=120):
        calls["count"] += 1
        raise _http_error(500, {"error": {"message": "boom", "code": "internal"}})

    monkeypatch.setenv("CONNECT_AGENT_MAX_RETRIES", "2")
    monkeypatch.setattr("framework.runtime.connect_agent.transport.urlopen", _urlopen)
    monkeypatch.setattr("framework.runtime.connect_agent.transport.time.sleep", lambda seconds: None)

    try:
        call_chat_completion(
            [{"role": "user", "content": "hello"}],
            model="gpt-5-mini",
            timeout=10,
        )
    except HTTPError as exc:
        assert exc.code == 500
    else:
        raise AssertionError("Expected HTTPError to be raised")

    assert calls["count"] == 1


def test_call_chat_completion_timeout_shuts_down_without_waiting(monkeypatch):
    from framework.runtime.connect_agent.transport import call_chat_completion

    shutdown_calls: list[tuple[bool, bool]] = []

    class _Future:
        def __init__(self):
            self.cancelled = False

        def result(self, timeout=None):
            raise FutureTimeoutError()

        def cancel(self):
            self.cancelled = True
            return True

    future = _Future()

    class _Executor:
        def __init__(self, max_workers=1):
            self.max_workers = max_workers

        def submit(self, fn, request, timeout):
            return future

        def shutdown(self, wait=True, cancel_futures=False):
            shutdown_calls.append((wait, cancel_futures))

    monkeypatch.setattr("framework.runtime.connect_agent.transport.ThreadPoolExecutor", _Executor)

    try:
        call_chat_completion(
            [{"role": "user", "content": "hello"}],
            model="gpt-5-mini",
            timeout=1,
        )
    except URLError as exc:
        assert "timed out" in str(exc.reason)
    else:
        raise AssertionError("Expected URLError to be raised")

    assert future.cancelled is True
    assert shutdown_calls == [(False, True)]


def test_call_chat_completion_debug_logs_request_lifecycle(monkeypatch, capsys):
    from framework.runtime.connect_agent.transport import call_chat_completion

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [{
                    "message": {"content": "ok"},
                    "finish_reason": "stop",
                }]
            }).encode("utf-8")

    def _urlopen(request, timeout=120):
        return _Response()

    monkeypatch.setenv("CONNECT_AGENT_DEBUG_LOG", "1")
    monkeypatch.setattr("framework.runtime.connect_agent.transport.urlopen", _urlopen)

    payload = call_chat_completion(
        [{"role": "user", "content": "hello"}],
        model="gpt-5-mini",
        timeout=10,
    )

    assert payload["choices"][0]["message"]["content"] == "ok"
    stderr = capsys.readouterr().err
    assert "request.start" in stderr
    assert "request.success" in stderr
    assert "gpt-5-mini" in stderr