"""Capability Registry HTTP API server for Constellation v2."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from urllib.parse import parse_qs, urlparse

from registry.store import store


HOST = os.environ.get("REGISTRY_HOST", "0.0.0.0")
PORT = int(os.environ.get("REGISTRY_PORT", "9000"))


def _parse_path(path: str) -> tuple[str | None, str | None, str | None, str | None]:
    if path == "/agents":
        return "agents", None, None, None
    if path == "/query":
        return "query", None, None, None
    if path == "/topology":
        return "topology", None, None, None
    if path == "/events":
        return "events", None, None, None
    match = re.match(r"^/agents/([^/]+)/instances/([^/]+)$", path)
    if match:
        return "instance", match.group(1), None, match.group(2)
    match = re.match(r"^/agents/([^/]+)/instances$", path)
    if match:
        return "instances", match.group(1), None, None
    match = re.match(r"^/agents/([^/]+)$", path)
    if match:
        return "agent", match.group(1), None, None
    return None, None, None, None


class RegistryHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, code: int, payload: dict | list) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self._send_json(200, {"status": "ok", "service": "registry"})
            return

        resource, agent_id, _, _ = _parse_path(path)

        if resource == "agents":
            self._send_json(200, [definition.to_dict() for definition in store.list_definitions()])
            return

        if resource == "agent":
            definition = store.get_definition(agent_id or "")
            if definition is None:
                self._send_json(404, {"error": "not_found", "agentId": agent_id})
                return
            self._send_json(200, definition.to_dict())
            return

        if resource == "instances":
            self._send_json(200, [instance.to_dict() for instance in store.list_instances(agent_id or "")])
            return

        if resource == "query":
            capability = parse_qs(parsed.query).get("capability", [""])[0]
            definitions = store.find_by_capability(capability) if capability else store.list_definitions(active_only=True)
            response = []
            for definition in definitions:
                payload = definition.to_dict()
                payload["instances"] = [instance.to_dict() for instance in store.list_instances(definition.agent_id, active_only=True)]
                response.append(payload)
            self._send_json(200, response)
            return

        if resource == "topology":
            self._send_json(200, store.topology_state())
            return

        if resource == "events":
            try:
                since_version = int(parse_qs(parsed.query).get("sinceVersion", ["0"])[0])
            except (TypeError, ValueError):
                since_version = 0
            topology = store.topology_state()
            self._send_json(200, {**topology, "events": store.list_events(since_version=since_version)})
            return

        self._send_json(404, {"error": "unknown_path"})

    def do_POST(self) -> None:
        resource, agent_id, _, _ = _parse_path(self.path)
        body = self._read_body()

        if resource == "agents":
            required = ("agentId", "version", "cardUrl", "capabilities")
            missing = [field for field in required if field not in body]
            if missing:
                self._send_json(400, {"error": "missing_fields", "fields": missing})
                return

            definition = store.register(
                agent_id=body["agentId"],
                version=body["version"],
                card_url=body["cardUrl"],
                capabilities=body["capabilities"],
                execution_mode=body.get("executionMode", "per-task"),
                scaling_policy=body.get("scalingPolicy"),
                launch_spec=body.get("launchSpec"),
                display_name=body.get("displayName"),
                description=body.get("description", ""),
                registered_by=body.get("registeredBy", "script"),
            )
            self._send_json(201, definition.to_dict())
            return

        if resource == "instances":
            instance = store.add_instance(
                agent_id=agent_id or "",
                service_url=body.get("serviceUrl", ""),
                port=int(body.get("port", 0) or 0),
                container_id=body.get("containerId", ""),
            )
            self._send_json(201, instance.to_dict())
            return

        self._send_json(404, {"error": "unknown_path"})

    def do_PUT(self) -> None:
        resource, agent_id, _, instance_id = _parse_path(self.path)
        if resource != "instance":
            self._send_json(404, {"error": "unknown_path"})
            return

        body = self._read_body()
        if body.get("heartbeat"):
            instance = store.heartbeat(agent_id or "", instance_id or "")
        else:
            instance = store.update_instance(agent_id or "", instance_id or "", **body)
        if instance is None:
            self._send_json(404, {"error": "instance_not_found"})
            return
        self._send_json(200, instance.to_dict())

    def log_message(self, fmt: str, *args) -> None:
        line = args[0] if args else ""
        if "/health" in line:
            return
        if line.startswith("PUT") and "/instances/" in line:
            return
        print(f"[registry] {line} {args[1] if len(args) > 1 else ''} {args[2] if len(args) > 2 else ''}")


def main() -> None:
    print(f"[registry] Starting registry on {HOST}:{PORT}")
    server = ThreadingHTTPServer((HOST, PORT), RegistryHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
