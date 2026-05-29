"""A2A HTTP server mixin — shared request handling for all agents.

Provides the mandatory A2A endpoints:
  GET  /health
  GET  /.well-known/agent-card.json
  POST /message:send
  GET  /tasks/{id}
  POST /tasks/{id}/callbacks
  POST /tasks/{id}/progress
  POST /tasks/{id}/ack
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlparse


class A2ARequestHandler(BaseHTTPRequestHandler):
    """Mixin providing A2A HTTP endpoints.

    Subclasses must set ``agent`` (a BaseAgent instance) and ``agent_card_path``
    on the handler class or on ``self.server``.
    """

    # Set by the server factory
    agent: Any = None
    advertised_url: str = ""
    agent_card_path: str = ""

    # -- HTTP verbs -----------------------------------------------------------

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/")

        if path == "/health":
            self._send_json(200, {
                "status": "ok",
                "service": self.agent.definition.agent_id if self.agent else "unknown",
            })
            return

        if path == "/.well-known/agent-card.json":
            self._handle_agent_card()
            return

        # GET /tasks/{id}
        if path.startswith("/tasks/"):
            parts = path.split("/")
            if len(parts) >= 3:
                task_id = parts[2]
                self._handle_get_task(task_id)
                return

        if self.agent and hasattr(self.agent, "serve_ui"):
            try:
                response = self.agent.serve_ui(path or "/")
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            if isinstance(response, dict):
                self._send_response(response)
                return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")

        if path == "/message:send":
            body = self._read_json_body()
            if body is None:
                return
            self._handle_message_send(body)
            return

        # POST /tasks/{id}/callbacks
        if path.endswith("/callbacks"):
            parts = path.split("/")
            if len(parts) >= 3:
                task_id = parts[2]
                body = self._read_json_body()
                if body is None:
                    return
                self._handle_callback(task_id, body)
                return

        # POST /tasks/{id}/progress
        if path.endswith("/progress"):
            parts = path.split("/")
            if len(parts) >= 3:
                task_id = parts[2]
                body = self._read_json_body()
                if body is None:
                    return
                self._handle_progress(task_id, body)
                return

        # POST /tasks/{id}/ack
        if path.endswith("/ack"):
            parts = path.split("/")
            if len(parts) >= 3:
                task_id = parts[2]
                self._handle_ack(task_id)
                return

        # POST /tasks/{id}/resume
        if path.endswith("/resume"):
            parts = path.split("/")
            if len(parts) >= 3:
                task_id = parts[2]
                body = self._read_json_body()
                if body is None:
                    return
                self._handle_resume(task_id, body)
                return

        self._send_json(404, {"error": "Not found"})

    # -- Endpoint handlers (override in subclasses if needed) ----------------

    def _handle_agent_card(self) -> None:
        card_path = self.agent_card_path
        if not card_path or not os.path.isfile(card_path):
            self._send_json(404, {"error": "Agent card not found"})
            return
        with open(card_path, encoding="utf-8") as fh:
            card = json.load(fh)
        text = json.dumps(card).replace("__ADVERTISED_URL__", self.advertised_url)
        self._send_json(200, json.loads(text))

    def _handle_message_send(self, body: dict) -> None:
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(self.agent.handle_message(body))
            self._send_json(200, result)
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})
        finally:
            loop.close()

    def _handle_get_task(self, task_id: str) -> None:
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(self.agent.get_task(task_id))
            self._send_json(200, result)
        except Exception as exc:
            self._send_json(404, {"error": str(exc)})
        finally:
            loop.close()

    def _handle_callback(self, task_id: str, body: dict) -> None:
        # Default: record callback metadata, write task-scoped logs, and return OK.
        agent_id = self.agent.definition.agent_id if self.agent else "unknown"
        try:
            from framework.devlog import AgentLogger

            log = AgentLogger(task_id=task_id, agent_name=agent_id)
            log.a2a(
                "←",
                "callback",
                state=body.get("state", ""),
                result_preview=str(body.get("result", ""))[:200],
            )
        except Exception:
            pass

        task_store = getattr(getattr(self.agent, "services", None), "task_store", None)
        if task_store is not None:
            try:
                task_store.update_metadata(task_id, {"last_callback": body})
            except Exception:
                pass

        print(f"[{agent_id}] Callback received for task {task_id}")
        self._send_json(200, {"status": "ok"})

    def _handle_progress(self, task_id: str, body: dict) -> None:
        agent_id = self.agent.definition.agent_id if self.agent else "unknown"
        step = body.get("step", "")
        try:
            from framework.devlog import AgentLogger

            AgentLogger(task_id=task_id, agent_name=agent_id).info(
                "[A2A] ← progress",
                step=step,
            )
        except Exception:
            pass
        print(f"[{agent_id}] Progress for task {task_id}: {step}")
        self._send_json(200, {"status": "ok"})

    def _handle_ack(self, task_id: str) -> None:
        agent_id = self.agent.definition.agent_id if self.agent else "unknown"
        try:
            from framework.devlog import AgentLogger

            AgentLogger(task_id=task_id, agent_name=agent_id).a2a("←", "ack")
        except Exception:
            pass
        print(f"[{agent_id}] ACK received for task {task_id}")
        self._send_json(200, {"status": "ok"})

    def _handle_resume(self, task_id: str, body: dict) -> None:
        """Resume a paused (INPUT_REQUIRED) task with user-provided input."""
        import asyncio

        agent_id = self.agent.definition.agent_id if self.agent else "unknown"
        resume_value = body.get("input", body.get("resume_value", ""))
        print(f"[{agent_id}] Resume request for task {task_id}")

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                self.agent.resume_task(task_id, resume_value)
            )
            self._send_json(200, result)
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})
        finally:
            loop.close()

    # -- Helpers --------------------------------------------------------------

    def _read_json_body(self) -> dict | None:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_json(400, {"error": "Empty body"})
            return None
        raw = self.rfile.read(content_length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON"})
            return None

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_response(self, response: dict) -> None:
        status = int(response.get("status", 200))
        headers = dict(response.get("headers") or {})
        body = response.get("body", "")

        # Streaming (SSE): body is a generator/iterator and Content-Type is text/event-stream
        ctype = str(headers.get("Content-Type", "")).lower()
        is_streaming = ctype.startswith("text/event-stream") and hasattr(body, "__iter__") and not isinstance(
            body, (str, bytes, dict, list)
        )
        if is_streaming:
            headers.setdefault("Content-Type", "text/event-stream; charset=utf-8")
            headers["Cache-Control"] = "no-cache"
            headers["Connection"] = "keep-alive"
            headers["X-Accel-Buffering"] = "no"
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                for chunk in body:
                    if chunk is None:
                        continue
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
            except Exception as exc:
                print(f"[a2a-sse] stream aborted: {exc}")
            return

        if isinstance(body, (dict, list)):
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json; charset=utf-8")
        elif isinstance(body, bytes):
            payload = body
        else:
            payload = str(body).encode("utf-8")
            headers.setdefault("Content-Type", "text/plain; charset=utf-8")

        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        """Prefix HTTP logs with agent ID."""
        agent_id = self.agent.definition.agent_id if self.agent else "a2a"
        print(f"[{agent_id}] {format % args}")
