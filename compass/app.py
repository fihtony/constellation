"""Compass agent with browser UI, workflow routing, and on-demand launcher support."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import threading
import time
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from common.artifact_store import ArtifactStore
from common.env_utils import load_dotenv
from common.launcher import Launcher
from common.message_utils import artifact_text, deep_copy_json, extract_text
from common.policy import PolicyEvaluator
from common.registry_client import RegistryClient
from common.task_store import TaskStore

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
ADVERTISED_URL = os.environ.get("ADVERTISED_BASE_URL", f"http://localhost:{PORT}")
ACK_TIMEOUT = int(os.environ.get("A2A_ACK_TIMEOUT_SECONDS", os.environ.get("A2A_READ_TIMEOUT_SECONDS", "15")))
DOWNSTREAM_TASK_TIMEOUT = int(os.environ.get("A2A_TASK_TIMEOUT_SECONDS", "3600"))
UI_PATH = os.path.join(os.path.dirname(__file__), "ui", "index.html")

registry = RegistryClient()
task_store = TaskStore()
artifact_store = ArtifactStore()
launcher = Launcher()
policy = PolicyEvaluator()

TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
NON_TERMINAL_TASK_STATES = {
    "SUBMITTED",
    "ROUTING",
    "DISPATCHED",
    "TASK_STATE_ACCEPTED",
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_RUNNING",
    "TASK_STATE_DISPATCHED",
}
CALLBACK_LOCK = threading.Lock()
CALLBACK_EVENTS = {}
CALLBACK_RESULTS = {}


def audit_log(event, **kwargs):
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **kwargs}
    print(f"[audit] {json.dumps(entry, ensure_ascii=False)}")


def _create_shared_workspace(task_id):
    workspace_root = os.path.join(artifact_store.root, "workspaces")
    os.makedirs(workspace_root, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    workspace_path = os.path.join(workspace_root, f"{task_id}-{timestamp}")
    os.makedirs(workspace_path, exist_ok=True)
    return workspace_path


def _read_agent_logs(since=0):
    generated_at = time.time()
    try:
        containers = launcher.list_agent_containers(include_stopped=True)
    except Exception as error:
        return {
            "generatedAt": generated_at,
            "agents": [],
            "error": str(error),
        }

    agents = []
    for container in containers:
        try:
            logs = launcher.read_container_logs(container["container_id"], since=since, tail=200)
        except Exception as error:
            logs = [{"ts": "", "line": f"[log_error] {error}"}]
        agents.append({
            "agentId": container["agent_id"],
            "displayName": container["display_name"],
            "role": container["role"],
            "state": container["state"],
            "status": container["status"],
            "containerId": container["container_id"],
            "containerName": container["container_name"],
            "taskId": container.get("task_id"),
            "logs": logs,
        })
    return {
        "generatedAt": generated_at,
        "agents": agents,
    }


def _a2a_call(agent_url, message, context_id=None):
    body = {
        "message": message,
        "configuration": {
            "returnImmediately": True,
            "acceptedOutputModes": ["text/plain"],
        },
    }
    if context_id:
        body["contextId"] = context_id

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{agent_url}/message:send",
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    print(f"[compass] Dispatching to {agent_url}/message:send")
    with urlopen(request, timeout=ACK_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_task(agent_url, task_id):
    request = Request(
        f"{agent_url.rstrip('/')}/tasks/{task_id}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urlopen(request, timeout=ACK_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _lookup_agents(requested_capability=None):
    try:
        if requested_capability:
            return registry.find_by_capability(requested_capability)
        return registry.find_any_active()
    except (URLError, OSError) as error:
        print(f"[compass] Registry unreachable: {error}")
        return None


def _find_idle_agent_and_instance(agents, container_name=None):
    for agent in agents:
        for instance in agent.get("instances", []):
            if container_name and instance.get("container_id") != container_name:
                continue
            if instance.get("status") == "idle":
                return agent, instance
    return None, None


def _extract_requested_capability(body, message):
    top_level = body.get("requestedCapability")
    if top_level:
        return top_level
    metadata = body.get("metadata", {})
    if metadata.get("requestedCapability"):
        return metadata["requestedCapability"]
    message_metadata = message.get("metadata", {})
    return message_metadata.get("requestedCapability")


def _dedupe(items):
    seen = set()
    ordered = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _infer_capability_workflow(user_text):
    lowered = (user_text or "").lower()
    workflow = []
    if TICKET_RE.search(user_text or ""):
        workflow.append("tracker.ticket.fetch")
    if any(term in lowered for term in ("scm", "repo", "repository", "pull request", "branch", "ios", "middleware")):
        workflow.append("scm.repo.inspect")
    if any(term in lowered for term in ("android", "implement", "implementation", "fix", "bug", "patch", "code", "build")):
        workflow.append("android.task.execute")
    if not workflow:
        workflow.append("scm.repo.inspect")
    return _dedupe(workflow)


def _wait_for_instance(agent_id, container_name, timeout_seconds=20):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        instances = registry.list_instances(agent_id)
        for instance in instances:
            if instance.get("container_id") == container_name and instance.get("status") == "idle":
                return instance
        time.sleep(0.5)
    return None


def _summarize_artifacts(agent_id, capability, artifacts):
    summaries = []
    for index, artifact in enumerate(artifacts, start=1):
        text = artifact_text(artifact)
        metadata = dict(artifact.get("metadata") or {})
        metadata.update({
            "agentId": agent_id,
            "capability": capability,
            "artifactName": artifact.get("name", f"artifact-{index}"),
            "index": index,
        })
        stored = artifact_store.store(
            metadata.get("orchestratorTaskId") or metadata.get("taskId") or "",
            artifact.get("artifactType", "a2a_artifact"),
            text or json.dumps(artifact, ensure_ascii=False),
            metadata=metadata,
        )
        summaries.append({
            "artifactId": stored.artifact_id,
            "agentId": agent_id,
            "capability": capability,
            "name": artifact.get("name", f"artifact-{index}"),
            "text": text,
            "metadata": metadata,
        })
    return summaries


def _store_task_artifacts(task_id, agent_id, capability, artifacts):
    summaries = []
    for index, artifact in enumerate(artifacts, start=1):
        text = artifact_text(artifact)
        metadata = dict(artifact.get("metadata") or {})
        metadata.update({
            "agentId": agent_id,
            "capability": capability,
            "artifactName": artifact.get("name", f"artifact-{index}"),
            "index": index,
            "orchestratorTaskId": task_id,
        })
        stored = artifact_store.store(
            task_id,
            artifact.get("artifactType", "a2a_artifact"),
            text or json.dumps(artifact, ensure_ascii=False),
            metadata=metadata,
        )
        summaries.append({
            "artifactId": stored.artifact_id,
            "agentId": agent_id,
            "capability": capability,
            "name": artifact.get("name", f"artifact-{index}"),
            "text": text,
            "metadata": metadata,
        })
    return summaries


def _append_task_artifacts(task, summaries):
    for summary in summaries:
        task.artifacts.append({
            "artifactId": summary["artifactId"],
            "name": summary["name"],
            "artifactType": "application/vnd.multi-agent.reference",
            "parts": [{"text": summary["text"]}],
            "metadata": summary["metadata"],
        })


def _build_step_message(task, original_message, task_id, capability, step_index, total_steps, upstream_artifacts):
    message = deep_copy_json(original_message)
    metadata = dict(message.get("metadata") or {})
    metadata.update({
        "requestedCapability": capability,
        "orchestratorTaskId": task_id,
        "orchestratorCallbackUrl": f"{ADVERTISED_URL.rstrip('/')}/tasks/{task_id}/callbacks",
        "sharedWorkspacePath": task.workspace_path,
        "workflowStep": step_index,
        "workflowTotalSteps": total_steps,
        "upstreamArtifacts": upstream_artifacts,
    })
    message["metadata"] = metadata
    return message


def _is_terminal_state(state):
    return (state or "TASK_STATE_COMPLETED") not in NON_TERMINAL_TASK_STATES


def _extract_downstream_result(downstream_task):
    state = downstream_task.get("status", {}).get("state", "TASK_STATE_COMPLETED")
    status_message = ""
    artifacts = downstream_task.get("artifacts", [])
    if artifacts:
        status_message = artifact_text(artifacts[0])
    if not status_message:
        status_message = extract_text(downstream_task.get("status", {}).get("message", {}))
    return {
        "state": state,
        "status_message": status_message,
        "artifacts": artifacts,
    }


def _callback_key(task_id, downstream_task_id):
    return f"{task_id}:{downstream_task_id}"


def _register_callback_waiter(task_id, downstream_task_id):
    key = _callback_key(task_id, downstream_task_id)
    event = threading.Event()
    with CALLBACK_LOCK:
        CALLBACK_EVENTS[key] = event
        if key in CALLBACK_RESULTS:
            event.set()
    return key, event


def _store_callback_result(task_id, downstream_task_id, payload):
    key = _callback_key(task_id, downstream_task_id)
    with CALLBACK_LOCK:
        CALLBACK_RESULTS[key] = payload
        event = CALLBACK_EVENTS.get(key)
    if event:
        event.set()


def _consume_callback_result(key):
    with CALLBACK_LOCK:
        CALLBACK_EVENTS.pop(key, None)
        return CALLBACK_RESULTS.pop(key, None)


def _cleanup_callback_waiter(key):
    with CALLBACK_LOCK:
        CALLBACK_EVENTS.pop(key, None)
        CALLBACK_RESULTS.pop(key, None)


def _wait_for_downstream_completion(task, agent_id, capability, service_url, downstream_task_id):
    key, event = _register_callback_waiter(task.task_id, downstream_task_id)
    deadline = time.time() + DOWNSTREAM_TASK_TIMEOUT
    next_poll_at = time.time()
    try:
        while time.time() < deadline:
            if event.wait(timeout=1.0):
                callback_result = _consume_callback_result(key)
                if callback_result:
                    return callback_result

            if time.time() >= next_poll_at:
                next_poll_at = time.time() + 5.0
                try:
                    response = _fetch_task(service_url, downstream_task_id)
                except Exception:
                    continue
                downstream_task = response.get("task", {})
                if not downstream_task:
                    continue
                polled_result = _extract_downstream_result(downstream_task)
                if _is_terminal_state(polled_result["state"]):
                    return polled_result

        task_store.update_state(
            task.task_id,
            "FAILED",
            f"Timed out waiting for {agent_id} to finish capability '{capability}'.",
        )
        return {
            "state": "FAILED",
            "status_message": f"Timed out waiting for {agent_id} to finish capability '{capability}'.",
            "artifacts": [],
        }
    finally:
        _cleanup_callback_waiter(key)


def _dispatch_step(task, original_message, capability, step_index, total_steps, upstream_artifacts):
    agents = _lookup_agents(capability)
    if agents is None:
        task_store.update_state(
            task.task_id,
            "CAPABILITY_TEMPORARILY_UNAVAILABLE",
            f"Registry unavailable while resolving capability '{capability}'.",
        )
        audit_log("REGISTRY_UNAVAILABLE", task_id=task.task_id, capability=capability)
        return {"terminal": True}

    if not agents:
        task_store.update_state(
            task.task_id,
            "NO_CAPABLE_AGENT",
            f"No active agent advertises capability '{capability}'.",
        )
        audit_log("NO_CAPABLE_AGENT", task_id=task.task_id, capability=capability)
        return {"terminal": True}

    agent, instance = _find_idle_agent_and_instance(agents)
    candidate = agents[0]

    if instance is None:
        if candidate.get("execution_mode") == "per-task":
            try:
                launch_info = launcher.launch_instance(candidate, task.task_id)
            except Exception as error:
                task_store.update_state(
                    task.task_id,
                    "CAPABILITY_TEMPORARILY_UNAVAILABLE",
                    f"Failed to launch capability '{capability}': {error}",
                )
                audit_log("LAUNCH_FAILED", task_id=task.task_id, capability=capability, error=str(error))
                return {"terminal": True}
            instance = _wait_for_instance(candidate["agent_id"], launch_info["container_name"])
            if instance is None:
                task_store.update_state(
                    task.task_id,
                    "CAPABILITY_TEMPORARILY_UNAVAILABLE",
                    f"Capability '{capability}' did not register an idle instance in time.",
                )
                audit_log("LAUNCH_TIMEOUT", task_id=task.task_id, capability=capability)
                return {"terminal": True}
            agent = candidate
        else:
            task_store.update_state(
                task.task_id,
                "CAPACITY_EXHAUSTED",
                f"Capability '{capability}' is registered but has no idle instances.",
            )
            audit_log("CAPACITY_EXHAUSTED", task_id=task.task_id, capability=capability)
            return {"terminal": True}

    policy_result = policy.evaluate(task.to_dict(), agent)
    if not policy_result.get("approved"):
        task_store.update_state(task.task_id, "POLICY_DENIED", policy_result.get("reason", ""))
        audit_log("POLICY_DENIED", task_id=task.task_id, capability=capability)
        return {"terminal": True}

    if agent is None or instance is None:
        task_store.update_state(
            task.task_id,
            "CAPABILITY_TEMPORARILY_UNAVAILABLE",
            f"Capability '{capability}' could not provide a routable instance.",
        )
        audit_log("ROUTE_INSTANCE_MISSING", task_id=task.task_id, capability=capability)
        return {"terminal": True}

    agent_id = agent["agent_id"]
    instance_id = instance["instance_id"]
    service_url = instance["service_url"]
    task_store.assign_agent(task.task_id, agent_id, instance_id)
    task_store.update_state(
        task.task_id,
        "DISPATCHED",
        f"Step {step_index}/{total_steps} dispatched to {agent_id} ({capability}).",
    )
    audit_log(
        "TASK_DISPATCHED",
        task_id=task.task_id,
        capability=capability,
        agent_id=agent_id,
        instance_id=instance_id,
        service_url=service_url,
    )

    try:
        registry.mark_instance_busy(agent_id, instance_id, task.task_id)
    except (URLError, OSError):
        pass

    try:
        step_message = _build_step_message(
            task,
            original_message,
            task.task_id,
            capability,
            step_index,
            total_steps,
            upstream_artifacts,
        )
        result = _a2a_call(service_url, step_message)
        downstream_task = result.get("task", {})
        downstream_task_id = downstream_task.get("id", "")
        extracted = _extract_downstream_result(downstream_task)
        state = extracted["state"]
        status_message = extracted["status_message"]
        artifacts = extracted["artifacts"]

        if downstream_task_id and not _is_terminal_state(state):
            task_store.update_state(
                task.task_id,
                "STEP_IN_PROGRESS",
                f"Step {step_index}/{total_steps} running in {agent_id} ({capability}).",
            )
            extracted = _wait_for_downstream_completion(
                task,
                agent_id,
                capability,
                service_url,
                downstream_task_id,
            )
            state = extracted["state"]
            status_message = extracted["status_message"]
            artifacts = extracted["artifacts"]

        summaries = _store_task_artifacts(task.task_id, agent_id, capability, artifacts)
        _append_task_artifacts(task, summaries)
        audit_log(
            "STEP_COMPLETED",
            task_id=task.task_id,
            capability=capability,
            agent_id=agent_id,
            state=state,
            artifact_count=len(summaries),
        )
        return {
            "terminal": False,
            "state": state,
            "status_message": status_message,
            "agent_id": agent_id,
            "artifact_summaries": summaries,
        }
    except Exception as error:
        task_store.update_state(task.task_id, "FAILED", f"Dispatch failed: {error}")
        audit_log("TASK_FAILED", task_id=task.task_id, capability=capability, error=str(error))
        return {"terminal": True}
    finally:
        try:
            registry.mark_instance_idle(agent_id, instance_id)
        except (URLError, OSError):
            pass


def _run_workflow(task_id, message, workflow):
    task = task_store.get(task_id)
    if not task:
        return
    upstream_artifacts = []
    final_state = "TASK_STATE_COMPLETED"
    final_message = "Workflow completed."

    for step_index, capability in enumerate(workflow, start=1):
        result = _dispatch_step(task, message, capability, step_index, len(workflow), upstream_artifacts)
        if result.get("terminal"):
            return task.to_dict()

        artifact_summaries = result.get("artifact_summaries")
        if not isinstance(artifact_summaries, list):
            artifact_summaries = []
        upstream_artifacts.extend(artifact_summaries)
        if step_index < len(workflow) and result["state"] == "TASK_STATE_COMPLETED":
            task_store.update_state(
                task.task_id,
                "STEP_COMPLETED",
                f"Step {step_index}/{len(workflow)} completed via {result['agent_id']}.",
            )
            continue

        final_state = result["state"]
        final_message = str(result.get("status_message") or f"Workflow finished via {result['agent_id']}.")
        if step_index < len(workflow):
            task_store.update_state(task.task_id, final_state, final_message)
            return task.to_dict()

    task_store.update_state(task.task_id, final_state, final_message)
    audit_log("TASK_COMPLETED", task_id=task.task_id, final_state=final_state)
    return task.to_dict()


def route_and_dispatch(message, requested_capability=None, forced_workflow=None):
    task = task_store.create()
    task.workspace_path = _create_shared_workspace(task.task_id)
    task.original_message = deep_copy_json(message)
    user_text = extract_text(message)
    workflow = (
        forced_workflow
        or ([requested_capability] if requested_capability else _infer_capability_workflow(user_text))
    )
    task.pending_workflow = list(workflow)
    audit_log(
        "TASK_CREATED",
        task_id=task.task_id,
        user_text=user_text[:200],
        workflow=workflow,
    )
    task_store.update_state(task.task_id, "ROUTING", f"Planned workflow: {', '.join(workflow)}")
    task_store.add_progress_step(
        task.task_id,
        f"Created shared workspace: {task.workspace_path}",
        agent_id="compass-agent",
    )
    worker = threading.Thread(
        target=_run_workflow,
        args=(task.task_id, deep_copy_json(message), list(workflow)),
        daemon=True,
    )
    worker.start()
    return task.to_dict()


class CompassHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code, html):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            try:
                with open(UI_PATH, "r", encoding="utf-8") as handle:
                    self._send_html(200, handle.read())
            except OSError as error:
                self._send_json(500, {"error": "ui_unavailable", "message": str(error)})
            return

        if path == "/health":
            self._send_json(200, {"status": "ok", "service": "compass"})
            return

        if path == "/.well-known/agent-card.json":
            card_path = os.path.join(os.path.dirname(__file__), "agent-card.json")
            with open(card_path, encoding="utf-8") as fh:
                card = json.load(fh)
            text = json.dumps(card).replace("__ADVERTISED_URL__", ADVERTISED_URL)
            self._send_json(200, json.loads(text))
            return

        if path == "/debug/agent-logs":
            query = parse_qs(urlparse(self.path).query)
            try:
                since = int(float(query.get("since", [0])[0]))
            except (TypeError, ValueError):
                since = 0
            self._send_json(200, _read_agent_logs(since=since))
            return

        if path.startswith("/tasks/"):
            suffix = path.split("/tasks/", 1)[1]
            if suffix.endswith("/artifacts"):
                task_id = suffix[:-len("/artifacts")]
                artifacts = artifact_store.get_by_task(task_id)
                self._send_json(200, {
                    "taskId": task_id,
                    "artifacts": [artifact.to_dict(include_content=True) for artifact in artifacts],
                })
                return

            task = task_store.get(suffix)
            if task:
                self._send_json(200, {"task": task.to_dict()})
            else:
                self._send_json(404, {"error": "task_not_found"})
            return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path

        # POST /tasks/{task_id}/progress — agents report major workflow steps
        m = re.fullmatch(r"/tasks/([^/]+)/progress", path)
        if m:
            task_id = m.group(1)
            body = self._read_body()
            step = (body.get("step") or "").strip()
            agent_id = body.get("agentId", "")
            ts = body.get("ts")
            if step:
                task_store.add_progress_step(task_id, step, agent_id=agent_id, ts=ts)
                print(f"[compass] Progress [{task_id}] <{agent_id}>: {step}")
            self._send_json(200, {"ok": True})
            return

        # POST /tasks/{task_id}/callbacks — downstream agents notify completion
        m = re.fullmatch(r"/tasks/([^/]+)/callbacks", path)
        if m:
            task_id = m.group(1)
            body = self._read_body()
            downstream_task_id = (body.get("downstreamTaskId") or body.get("taskId") or "").strip()
            if not downstream_task_id:
                self._send_json(400, {"error": "missing_downstream_task_id"})
                return
            payload = {
                "state": body.get("state", "TASK_STATE_COMPLETED"),
                "status_message": body.get("statusMessage", ""),
                "artifacts": body.get("artifacts") or [],
                "agent_id": body.get("agentId", ""),
            }
            _store_callback_result(task_id, downstream_task_id, payload)
            audit_log(
                "TASK_CALLBACK_RECEIVED",
                task_id=task_id,
                downstream_task_id=downstream_task_id,
                agent_id=payload["agent_id"],
                state=payload["state"],
            )
            self._send_json(200, {"ok": True})
            return

        if path != "/message:send":
            self._send_json(404, {"error": "not_found"})
            return

        body = self._read_body()
        message = body.get("message", {})
        requested_capability = _extract_requested_capability(body, message)
        if not message:
            self._send_json(400, {"error": "missing message"})
            return

        # If the caller supplies a contextId pointing to a TASK_STATE_INPUT_REQUIRED task,
        # merge the user's new text with the original message and re-run the same workflow.
        context_id = (body.get("contextId") or "").strip()
        if context_id:
            prior_task = task_store.get(context_id)
            if prior_task and prior_task.state == "TASK_STATE_INPUT_REQUIRED":
                orig_text = extract_text(prior_task.original_message or {})
                new_text = extract_text(message)
                combined_text = (orig_text + "\n\n" + new_text).strip() if orig_text else new_text
                merged = deep_copy_json(message)
                merged["parts"] = [{"text": combined_text}]
                workflow = prior_task.pending_workflow
                print(f"[compass] Resuming INPUT_REQUIRED task {context_id} with merged message")
                task_dict = route_and_dispatch(merged, forced_workflow=workflow)
                self._send_json(200, {"task": task_dict})
                return

        print(f"[compass] Received message: {json.dumps(message, ensure_ascii=False)[:200]}")
        task_dict = route_and_dispatch(message, requested_capability=requested_capability)
        self._send_json(200, {"task": task_dict})

    def log_message(self, fmt, *args):
        # Suppress noisy health-check and agent-card polls
        line = args[0] if args else ""
        if any(p in line for p in ("/health", "/.well-known/agent-card.json")):
            return
        print(f"[compass] {line} {args[1] if len(args) > 1 else ''} {args[2] if len(args) > 2 else ''}")


def main():
    print(f"[compass] Compass agent starting on {HOST}:{PORT}")
    print(f"[compass] Artifact root: {artifact_store.root}")
    server = ThreadingHTTPServer((HOST, PORT), CompassHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()