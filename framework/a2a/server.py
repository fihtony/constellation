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
        # Default: log and return OK.  Override for actual callback handling.
        agent_id = self.agent.definition.agent_id if self.agent else "unknown"
        print(f"[{agent_id}] Callback received for task {task_id}")
        self._send_json(200, {"status": "ok"})

    def _handle_progress(self, task_id: str, body: dict) -> None:
        agent_id = self.agent.definition.agent_id if self.agent else "unknown"
        step = body.get("step", "")
        print(f"[{agent_id}] Progress for task {task_id}: {step}")
        self._send_json(200, {"status": "ok"})

    def _handle_ack(self, task_id: str) -> None:
        agent_id = self.agent.definition.agent_id if self.agent else "unknown"
        print(f"[{agent_id}] ACK received for task {task_id}")
        self._send_json(200, {"status": "ok"})

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

    def log_message(self, format: str, *args: Any) -> None:
        """Prefix HTTP logs with agent ID."""
        agent_id = self.agent.definition.agent_id if self.agent else "a2a"
        print(f"[{agent_id}] {format % args}")
