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
            artifact_error = self._artifact_root_access_error()
            if artifact_error:
                self._send_json(503, {
                    "status": "error",
                    "service": self.agent.definition.agent_id if self.agent else "unknown",
                    "artifactRootAccessible": False,
                    "error": artifact_error,
                })
                return
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

        # POST /tasks/{id}/child-timeout
        if path.endswith("/child-timeout"):
            parts = path.split("/")
            if len(parts) >= 3:
                task_id = parts[2]
                body = self._read_json_body()
                if body is None:
                    return
                self._handle_child_timeout(task_id, body)
                return

        # POST /tasks/{id}/ack
        if path.endswith("/ack"):
            parts = path.split("/")
            if len(parts) >= 3:
                task_id = parts[2]
                self._handle_ack(task_id)
                return

        # POST /tasks/{id}/ping
        if path.endswith("/ping"):
            parts = path.split("/")
            if len(parts) >= 3:
                task_id = parts[2]
                self._handle_ping(task_id)
                return

        # POST /tasks/{id}/terminate
        if path.endswith("/terminate"):
            parts = path.split("/")
            if len(parts) >= 3:
                task_id = parts[2]
                self._handle_terminate(task_id)
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

        # POST /tasks/{id}/cancel — task-scoped cancellation. Distinct
        # from ``/terminate`` (which kills the agent process). Works in
        # any non-terminal state (SUBMITTED / WORKING / INPUT_REQUIRED);
        # returns ``already_terminal`` for tasks that are already
        # COMPLETED / FAILED / CANCELLED.
        if path.endswith("/cancel"):
            parts = path.split("/")
            if len(parts) >= 3:
                task_id = parts[2]
                body = self._read_json_body() or {}
                self._handle_cancel(task_id, body)
                return

        # POST /_major_step/events — downstream agent events fan-in.
        # The body MUST contain ``task_id`` (the Compass top-level task id);
        # ``step_key``, ``title``, ``agent`` and ``lifecycle_state`` are
        # forwarded to ``framework.major_step.record_major_step``. The path
        # matches ``framework.major_step.DEFAULT_SINK_PATH`` so the
        # ``resolve_progress_sink`` resolver appends the right suffix.
        if path == "/_major_step/events":
            body = self._read_json_body()
            if body is None:
                return
            self._handle_major_step_event(body)
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

    def _artifact_root_access_error(self) -> str:
        artifact_path = (
            os.environ.get("CONSTELLATION_TASK_WORKSPACE", "").strip()
            or os.environ.get("ARTIFACT_ROOT", "").strip()
        )
        if not artifact_path:
            return ""
        try:
            os.listdir(artifact_path)
        except OSError as exc:
            return f"Artifact workspace inaccessible: {artifact_path}: {exc}"
        if not os.access(artifact_path, os.R_OK | os.W_OK | os.X_OK):
            return f"Artifact workspace not readable/writable: {artifact_path}"
        return ""

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
        # Default: record callback metadata, write task-scoped logs, and — when
        # the body carries a recognized terminal ``state`` — transition the
        # local task store to match.  This is what makes fire-and-forget
        # dispatch (e.g. compass → office) actually drive the orchestrator's
        # task state machine: office POSTs ``{state: "completed", result: {...}}``
        # when its workflow finishes, and the compass task flips to
        # ``TASK_STATE_COMPLETED`` here, which the UI then renders.
        agent_id = self.agent.definition.agent_id if self.agent else "unknown"
        state = str(body.get("state") or "").strip().lower()
        result = body.get("result") or {}
        if not isinstance(result, dict):
            result = {}

        try:
            from framework.devlog import AgentLogger

            log = AgentLogger(task_id=task_id, agent_name=agent_id)
            log.a2a(
                "←",
                "callback",
                state=state,
                result_preview=str(result)[:200],
            )
        except Exception:
            pass

        task_store = getattr(getattr(self.agent, "services", None), "task_store", None)
        if task_store is not None:
            try:
                task_store.update_metadata(task_id, {"last_callback": body})
            except Exception:
                pass
            # Promote callback state into the task_store state machine when
            # the body is well-formed.  Failures are logged but never block
            # the HTTP response — the caller is fire-and-forget and the
            # background worker (if any) is the source of truth.
            try:
                if state in {"completed", "succeeded", "success"}:
                    summary = (
                        str(result.get("summary") or "").strip()
                        or f"{agent_id} reported completion via callback"
                    )
                    task_store.complete_task(task_id, message=summary)
                elif state in {"failed", "error"}:
                    summary = (
                        str(result.get("summary") or result.get("message") or "").strip()
                        or f"{agent_id} reported failure via callback"
                    )
                    task_store.fail_task(task_id, summary)
                elif state in {"input-required", "input_required", "waiting"}:
                    question = str(
                        result.get("question")
                        or result.get("summary")
                        or result.get("message")
                        or ""
                    ).strip()
                    task_store.pause_task(
                        task_id,
                        question=question or f"{agent_id} needs more input",
                    )
            except Exception as exc:
                # Callback is best-effort: never 500 the caller over a
                # promotion failure (the background worker is the real
                # source of truth for terminal state).
                print(f"[{agent_id}] callback state promotion failed: {exc}")

        print(f"[{agent_id}] Callback received for task {task_id} state={state!r}")
        self._send_json(200, {"status": "ok", "state": state})

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

    def _handle_major_step_event(self, body: dict) -> None:
        """Handle a fan-in major-step event from a downstream agent.

        The body must carry the structured fields from
        ``framework.major_step.record_major_step``. We resolve the local
        ``TaskStore`` from the agent and apply the event against the
        Compass task id (``task_id`` in the body). Returns 404 if the task
        is unknown, 200 with the merged event otherwise. Terminal-protection
        and idempotence on ``(step_key, round)`` are enforced inside
        ``record_major_step``.
        """
        task_id = str(body.get("task_id") or "").strip()
        if not task_id:
            self._send_json(400, {"error": "task_id required"})
            return
        agent = self.agent
        if agent is None or not hasattr(agent, "services"):
            self._send_json(503, {"error": "agent not initialized"})
            return
        task_store = agent.services.task_store
        task = task_store.get_task(task_id)
        if task is None:
            self._send_json(404, {"error": "task not found"})
            return
        try:
            from framework.major_step import record_major_step

            event = record_major_step(
                task_id,
                step_key=body.get("step_key", ""),
                title=body.get("title", ""),
                agent=body.get("agent", ""),
                lifecycle_state=body.get("lifecycle_state", "running"),
                visual_state=body.get("visual_state"),
                summary_template=body.get("summary_template", ""),
                summary_facts=body.get("summary_facts"),
                round=int(body.get("round", 0) or 0),
                conditional=bool(body.get("conditional", False)),
                task_store=task_store,
            )
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": f"failed to record step: {exc}"})
            return
        self._send_json(200, {"status": "ok", "event": event})

    def _handle_child_timeout(self, task_id: str, body: dict) -> None:
        agent_id = self.agent.definition.agent_id if self.agent else "unknown"
        child_agent_id = body.get("childAgentId", "")
        child_task_id = body.get("childTaskId", "")
        exit_code = body.get("exitCode", "")
        try:
            from framework.devlog import AgentLogger

            AgentLogger(task_id=task_id, agent_name=agent_id).warn(
                "[A2A] ← child-timeout",
                child_agent_id=child_agent_id,
                child_task_id=child_task_id,
                exit_code=exit_code,
            )
        except Exception:
            pass

        task_store = getattr(getattr(self.agent, "services", None), "task_store", None)
        if task_store is not None:
            try:
                task_store.update_metadata(task_id, {"last_child_timeout": body})
            except Exception:
                pass

        print(
            f"[{agent_id}] Child timeout reported for task {task_id}: "
            f"child_agent={child_agent_id} child_task={child_task_id} exit_code={exit_code}"
        )
        self._send_json(200, {"status": "ok"})

    def _handle_ack(self, task_id: str) -> None:
        agent_id = self.agent.definition.agent_id if self.agent else "unknown"
        try:
            from framework.devlog import AgentLogger

            AgentLogger(task_id=task_id, agent_name=agent_id).a2a("←", "ack")
        except Exception:
            pass
        print(f"[{agent_id}] ACK received for task {task_id}")

        # Delegate to lifecycle manager if available
        lifecycle = getattr(self.agent, "_lifecycle", None)
        if lifecycle is not None:
            result = lifecycle.handle_ack(task_id)
            self._send_json(200, result)
            return
        self._send_json(200, {"status": "ok"})

    def _handle_ping(self, task_id: str) -> None:
        agent_id = self.agent.definition.agent_id if self.agent else "unknown"
        print(f"[{agent_id}] Ping received for task {task_id}")

        # Delegate to lifecycle manager if available
        lifecycle = getattr(self.agent, "_lifecycle", None)
        if lifecycle is not None:
            result = lifecycle.handle_ping(task_id)
            self._send_json(200, result)
            return
        self._send_json(200, {"status": "ok"})

    def _handle_terminate(self, task_id: str) -> None:
        agent_id = self.agent.definition.agent_id if self.agent else "unknown"
        print(f"[{agent_id}] Terminate received for task {task_id}")

        # Delegate to lifecycle manager if available
        lifecycle = getattr(self.agent, "_lifecycle", None)
        if lifecycle is not None:
            result = lifecycle.handle_terminate(task_id)
            self._send_json(200, result)
            return
        self._send_json(200, {"status": "ok", "message": "no lifecycle manager"})

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

    def _handle_cancel(self, task_id: str, body: dict) -> None:
        """Handle a task-scoped cancel request.

        Distinct from ``/terminate`` (which shuts down the agent process
        via the lifecycle manager). Cancel works in any non-terminal
        state and propagates to downstream agents (e.g. compass →
        office) over A2A. The actual signal delivery to in-flight
        workflow threads happens inside ``agent.handle_task_cancel``.
        """
        import asyncio

        agent_id = self.agent.definition.agent_id if self.agent else "unknown"
        reason = str(body.get("reason") or "cancelled by user")
        print(f"[{agent_id}] Cancel request for task {task_id}")

        task_store = getattr(
            getattr(self.agent, "services", None), "task_store", None
        )
        if task_store is None:
            self._send_json(500, {"error": "task_store unavailable"})
            return

        task = task_store.get_task(task_id)
        if task is None:
            self._send_json(404, {"error": "not_found", "task_id": task_id})
            return

        current_state = getattr(
            getattr(task.status, "state", None), "value",
            str(getattr(task.status, "state", "")),
        )
        if current_state in {
            "TASK_STATE_COMPLETED",
            "TASK_STATE_FAILED",
            "TASK_STATE_CANCELLED",
        }:
            self._send_json(200, {
                "status": "already_terminal",
                "task_id": task_id,
                "task_state": current_state,
            })
            return

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                self.agent.handle_task_cancel(task_id, reason)
            )
        except AttributeError:
            # Agent does not implement handle_task_cancel — fall back to
            # the base behavior (mark the local task CANCELLED).
            task_store.cancel_task(task_id, reason)
            result = {"status": "ok", "task_id": task_id, "mode": "local_only"}
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})
            return
        finally:
            loop.close()

        # If the agent's handler already did the cancel_task transition,
        # ``result`` is informational. If the handler is missing, we did
        # it above. Either way, return 200 with the structured response.
        self._send_json(200, {
            "status": "ok",
            "task_id": task_id,
            "task_state": "TASK_STATE_CANCELLED",
            **(result or {}),
        })

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
