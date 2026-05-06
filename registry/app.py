"""Capability Registry HTTP API server."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from urllib.parse import parse_qs, urlparse

from common.registry_store import RegistryStore
from common.skills_catalog import SkillsCatalog

HOST = os.environ.get("REGISTRY_HOST", "0.0.0.0")
PORT = int(os.environ.get("REGISTRY_PORT", "9000"))

store = RegistryStore()
_skills = SkillsCatalog()


def _parse_path(path):
    # Skills catalog endpoints
    if path == "/skills/catalog":
        return "skills_catalog", None, None, None
    if path == "/skills/catalog/version":
        return "skills_catalog_version", None, None, None
    if path == "/skills/query":
        return "skills_query", None, None, None
    m = re.match(r"^/skills/([^/]+)$", path)
    if m:
        return "skill", m.group(1), None, None
    if path == "/topology":
        return "topology", None, None, None
    if path == "/events":
        return "events", None, None, None
    match = re.match(r"^/agents/([^/]+)/instances/([^/]+)$", path)
    if match:
        return "instance", match.group(1), "instances", match.group(2)
    match = re.match(r"^/agents/([^/]+)/instances$", path)
    if match:
        return "instances", match.group(1), None, None
    match = re.match(r"^/agents/([^/]+)$", path)
    if match:
        return "agent", match.group(1), None, None
    if path == "/agents":
        return "agents", None, None, None
    if path.startswith("/query"):
        return "query", None, None, None
    return None, None, None, None


class RegistryHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self._send_json(200, {"status": "ok", "service": "capability-registry"})
            return

        resource, agent_id, _, _ = _parse_path(path)

        if resource == "agents":
            definitions = store.list_definitions()
            self._send_json(200, [definition.to_dict() for definition in definitions])
            return

        if resource == "agent":
            definition = store.get_definition(agent_id)
            if definition is None:
                self._send_json(404, {"error": "not_found", "agent_id": agent_id})
            else:
                self._send_json(200, definition.to_dict())
            return

        if resource == "instances":
            instances = store.list_instances(agent_id)
            self._send_json(200, [instance.to_dict() for instance in instances])
            return

        if resource == "query":
            capability = parse_qs(parsed.query).get("capability", [None])[0]
            definitions = store.find_by_capability(capability) if capability else store.find_any_active()
            response = []
            for definition in definitions:
                payload = definition.to_dict()
                payload["instances"] = [instance.to_dict() for instance in store.list_instances(definition.agent_id)]
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
            self._send_json(
                200,
                {
                    **topology,
                    "events": store.list_events(since_version=since_version),
                },
            )
            return

        if resource == "skills_catalog":
            self._send_json(200, {
                "version": _skills.get_version(),
                "skills": _skills.get_catalog(),
            })
            return

        if resource == "skills_catalog_version":
            self._send_json(200, {"version": _skills.get_version()})
            return

        if resource == "skill":
            # agent_id holds the skill id in this context (reused slot)
            s = _skills.get_skill(agent_id)
            if s is None:
                self._send_json(404, {"error": "skill_not_found", "skillId": agent_id})
            else:
                self._send_json(200, s)
            return

        self._send_json(404, {"error": "unknown_path"})

    def do_POST(self):
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
            print(f"[registry] Registered agent: {definition.agent_id} caps={definition.capabilities}")
            self._send_json(201, definition.to_dict())
            return

        if resource == "instances":
            instance = store.add_instance(
                agent_id=agent_id,
                service_url=body.get("serviceUrl", ""),
                port=body.get("port", 0),
                container_id=body.get("containerId"),
            )
            print(f"[registry] Instance added: {agent_id}/{instance.instance_id} url={instance.service_url}")
            self._send_json(201, instance.to_dict())
            return

        if resource == "skills_query":
            result = _skills.query(body)
            self._send_json(200, result)
            return

        self._send_json(404, {"error": "unknown_path"})

    def do_PUT(self):
        resource, agent_id, _, instance_id = _parse_path(self.path)
        body = self._read_body()

        if resource != "instance":
            self._send_json(404, {"error": "unknown_path"})
            return

        if body.get("heartbeat"):
            instance = store.heartbeat(agent_id, instance_id)
        else:
            instance = store.update_instance(agent_id, instance_id, **body)
        if instance is None:
            self._send_json(404, {"error": "instance_not_found"})
            return
        self._send_json(200, instance.to_dict())

    def do_DELETE(self):
        resource, agent_id, _, instance_id = _parse_path(self.path)

        if resource == "agent":
            definition = store.deregister(agent_id, deregistered_by="script")
            if definition is None:
                self._send_json(404, {"error": "not_found"})
            else:
                print(f"[registry] Deregistered agent: {agent_id}")
                self._send_json(200, definition.to_dict())
            return

        if resource == "instance":
            instance = store.remove_instance(agent_id, instance_id)
            if instance is None:
                self._send_json(404, {"error": "instance_not_found"})
            else:
                self._send_json(200, instance.to_dict())
            return

        self._send_json(404, {"error": "unknown_path"})

    def log_message(self, fmt, *args):
        line = args[0] if args else ""
        # Suppress health-checks, agent-card polls, and heartbeat PUT requests
        if any(p in line for p in ("/health", "/.well-known/agent-card.json")):
            return
        # Suppress heartbeat PUT requests (PUT /agents/.../instances/...)
        if line.startswith("PUT") and "/instances/" in line:
            return
        print(f"[registry] {line} {args[1] if len(args) > 1 else ''} {args[2] if len(args) > 2 else ''}")


def main():
    print(f"[registry] Capability Registry starting on {HOST}:{PORT}")
    _skills.scan()
    print(f"[registry] Skills catalog loaded: {len(_skills.get_catalog())} skills, version={_skills.get_version()}")
    server = ThreadingHTTPServer((HOST, PORT), RegistryHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()