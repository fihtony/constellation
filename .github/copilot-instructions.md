# Constellation — GitHub Copilot Instructions

## Project Overview

**Constellation** is a multi-agent system built on the A2A (Agent-to-Agent) protocol.
The system is named after the idea that each agent is a "star" — independent but connected,
working together to complete complex engineering tasks.

### Core Components

| Component | Directory | Role |
|-----------|-----------|------|
| **Compass Agent** | `compass/` | Control-plane entry point. Routes tasks, manages workflow, owns the Web UI. This is the "north star" that directs all other agents. |
| **Capability Registry** | `registry/` | Agent discovery and instance tracking. All agents register here on startup. |
| **Tracker Agent** | `tracker/` | Integrates with Jira-compatible systems. Fetches tickets, updates status, posts comments. |
| **SCM Agent** | `scm/` | Integrates with Git SCM (Bitbucket/GitHub). Repo inspection, branch, PR operations. |
| **Android Agent** | `android/` | On-demand execution agent. Launched per-task by the Compass Agent via Docker socket. |
| **Common Library** | `common/` | Shared modules: registry client, launcher, LLM client, artifact store, task store, etc. |

### MCP Tool Integrations (replacing standalone agents)

The system uses MCP (Model Context Protocol) servers instead of dedicated agents for:
- **Jira MCP** — replaces the old Jira Agent. Used by the Team Lead Agent (NOT directly by Compass).
- **GitHub MCP** — replaces the old Bitbucket Agent. Used by execution agents.
- **Google Stitch MCP** — provides design context. Used by execution agents.

### Architecture: Compass + Team Lead Pattern

```
User
  └─► Compass Agent (control plane, workflow routing)
         └─► Team Lead Agent (task analysis, MCP coordination)
                ├─► Jira MCP (fetch ticket details, update status)
                ├─► GitHub MCP (repo inspection, PR operations)
                ├─► Google Stitch MCP (design context)
                ├─► Android Agent (per-task container)
                ├─► iOS Agent (per-task container, future)
                └─► Middleware Agent (per-task container, future)
```

**Design Rationale**: Compass Agent should NOT call Jira MCP directly.
The Team Lead Agent is responsible for:
1. Fetching and analyzing Jira tickets via Jira MCP
2. Understanding the codebase via GitHub MCP
3. Breaking down tasks and delegating to execution agents
4. Aggregating results and reporting back to Compass

This separation keeps Compass thin (routing only) and puts domain intelligence in Team Lead.

### Container Runtime

This project uses **Docker Desktop**.
- Host machine is accessible from containers at `host.docker.internal`
- Docker socket: `/var/run/docker.sock`
- Network name: `mvp-network`
- LLM endpoint example: `http://host.docker.internal:1288/v1`

---

## Agent Development Guidelines

Every new agent in the Constellation system MUST follow the rules below.
These rules ensure all agents can discover each other, communicate correctly,
and participate in multi-step workflows.

### 1. Directory Structure

Each agent lives in its own top-level directory:

```
<agent-name>/
├── app.py                  # Agent HTTP server (REQUIRED)
├── Dockerfile              # Container build (REQUIRED)
├── agent-card.json         # A2A agent card — capability advertisement (REQUIRED)
├── registry-config.json    # Registry registration config (REQUIRED)
├── .env.example            # Environment variable template (REQUIRED)
└── __init__.py             # Python package marker (REQUIRED)
```

### 2. Mandatory A2A Interface

Every agent MUST expose these HTTP endpoints:

#### `GET /health`
Returns `{"status": "ok", "service": "<agent-id>"}` with HTTP 200.
Used by docker-compose healthcheck and the Compass Agent to verify the agent is alive.

```python
# Example
if path == "/health":
    self._send_json(200, {"status": "ok", "service": "my-agent"})
    return
```

#### `GET /.well-known/agent-card.json`
Returns the agent's capability card. Reads from `agent-card.json` on disk.
Used by the Registry bootstrap and other agents for discovery.

```python
if path == "/.well-known/agent-card.json":
    card_path = os.path.join(os.path.dirname(__file__), "agent-card.json")
    with open(card_path, encoding="utf-8") as fh:
        card = json.load(fh)
    text = json.dumps(card).replace("__ADVERTISED_URL__", ADVERTISED_URL)
    self._send_json(200, json.loads(text))
    return
```

#### `POST /message:send`
The main A2A dispatch endpoint. Accepts a task message and returns immediately
with a task object (containing a task ID). Processing continues asynchronously.

**Request body:**
```json
{
  "message": {
    "messageId": "unique-id",
    "role": "ROLE_USER",
    "parts": [{"text": "user request text"}],
    "metadata": {
      "requestedCapability": "my.skill.id",
      "orchestratorTaskId": "parent-task-id",
      "orchestratorCallbackUrl": "http://compass:8080/tasks/{id}/callbacks",
      "sharedWorkspacePath": "/app/artifacts/workspaces/task-xxx"
    }
  },
  "configuration": {
    "returnImmediately": true
  }
}
```

**Response body (HTTP 200):**
```json
{
  "task": {
    "id": "task-0001",
    "status": {"state": "TASK_STATE_WORKING"},
    "artifacts": []
  }
}
```

#### `GET /tasks/{task_id}`
Returns the current state of a task. Used by the Compass Agent for polling fallback.

**Response body (HTTP 200):**
```json
{
  "task": {
    "id": "task-0001",
    "status": {
      "state": "TASK_STATE_COMPLETED",
      "message": {"parts": [{"text": "Done."}]}
    },
    "artifacts": [...]
  }
}
```

### 3. agent-card.json

```json
{
  "schemaVersion": "1.0",
  "name": "My Agent",
  "description": "What this agent does, one sentence.",
  "version": "1.0.0",
  "supportedInterfaces": [
    {
      "url": "__ADVERTISED_URL__",
      "protocolBinding": "HTTP+JSON",
      "protocolVersion": "1.0"
    }
  ],
  "capabilities": {
    "streaming": false,
    "pushNotifications": false
  },
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"],
  "skills": [
    {
      "id": "my.skill.id",
      "name": "Human-readable skill name",
      "description": "What this skill does.",
      "tags": ["tag1", "tag2"]
    }
  ]
}
```

**Rules:**
- `__ADVERTISED_URL__` is a placeholder replaced at runtime with `ADVERTISED_BASE_URL`.
- `skills[].id` is the capability identifier used by Compass to route tasks.
- Skill IDs follow dot-notation: `<domain>.<resource>.<action>` (e.g., `tracker.ticket.fetch`).

### 4. registry-config.json

```json
{
  "agentId": "my-agent",
  "cardUrl": "http://my-agent:8080/.well-known/agent-card.json",
  "executionMode": "persistent",
  "scalingPolicy": {
    "maxInstances": 1,
    "perInstanceConcurrency": 1,
    "idleTimeoutSeconds": 300
  }
}
```

**`executionMode` values:**
- `"persistent"` — agent is always running (tracker, scm, compass). Registered once at startup.
- `"per-task"` — agent is launched on demand by Compass via Docker socket (android, ios). Must include `launchSpec`.

**For `per-task` agents, add `launchSpec`:**
```json
{
  "agentId": "android-agent",
  "executionMode": "per-task",
  "launchSpec": {
    "image": "mvp-android-agent:latest",
    "port": 8000
  },
  "scalingPolicy": {
    "maxInstances": 3,
    "perInstanceConcurrency": 1,
    "idleTimeoutSeconds": 120
  }
}
```

### 5. Instance Registration and Heartbeat

Persistent agents MUST use `common/instance_reporter.py` to register their instance
with the Capability Registry on startup and maintain heartbeat.

```python
from common.instance_reporter import InstanceReporter

reporter = InstanceReporter(
    agent_id=AGENT_ID,
    instance_id=INSTANCE_ID,
    service_url=ADVERTISED_URL,
    registry_url=REGISTRY_URL,
    heartbeat_interval=int(os.environ.get("HEARTBEAT_INTERVAL", "30")),
)
reporter.start()  # starts background heartbeat thread
```

The reporter handles:
- Initial registration on `start()`
- Periodic heartbeat to keep the instance marked `idle` in the registry
- `mark_busy(task_id)` / `mark_idle()` — call these at the start and end of each task

**For per-task agents**: registration happens automatically via Docker labels at container launch.
The agent must still call `mark_busy` / `mark_idle` via the registry HTTP API.

### 6. Task Lifecycle and State Machine

Task states (in order):
```
SUBMITTED → ROUTING → DISPATCHED → TASK_STATE_WORKING → TASK_STATE_COMPLETED
                                                       → TASK_STATE_FAILED
                                                       → TASK_STATE_INPUT_REQUIRED
```

**Rules:**
- On receiving `POST /message:send`, return immediately with state `TASK_STATE_WORKING` or `SUBMITTED`.
- Processing MUST run in a background thread (not the HTTP handler thread).
- On completion, update task state to `TASK_STATE_COMPLETED` or `TASK_STATE_FAILED`.
- All state transitions must be logged via `print()` with the `[agent-id]` prefix.

```python
# Example background processing pattern
import threading

def _process_task(task_id, message):
    try:
        result = do_work(message)
        task_store.update_state(task_id, "TASK_STATE_COMPLETED", result)
    except Exception as error:
        task_store.update_state(task_id, "TASK_STATE_FAILED", str(error))
    finally:
        reporter.mark_idle()

def handle_message(message):
    task = task_store.create()
    reporter.mark_busy(task.task_id)
    worker = threading.Thread(target=_process_task, args=(task.task_id, message), daemon=True)
    worker.start()
    return task.to_dict()
```

### 7. Async Skill Contracts and Callbacks

When a downstream agent takes a long time (> ACK timeout), it MUST support the
**callback pattern** to notify Compass when work is complete.

**Callback URL** is passed in `message.metadata.orchestratorCallbackUrl`.

```python
# When task completes, POST to callback URL
import json
from urllib.request import Request, urlopen

def _notify_compass(callback_url, task_id, state, status_message, artifacts):
    payload = {
        "downstreamTaskId": task_id,
        "state": state,
        "statusMessage": status_message,
        "artifacts": artifacts,
        "agentId": AGENT_ID,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        callback_url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10):
            pass
    except Exception as error:
        print(f"[my-agent] Callback failed: {error}")
```

**Compass fallback**: If no callback arrives within the timeout, Compass polls
`GET /tasks/{task_id}` every 5 seconds. Agents must support this endpoint.

### 8. Progress Reporting

Agents SHOULD report major workflow steps to Compass via the progress endpoint.
This is displayed in the Web UI timeline.

```python
def _report_progress(compass_url, task_id, step_text, agent_id):
    payload = {
        "step": step_text,
        "agentId": agent_id,
    }
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{compass_url}/tasks/{task_id}/progress",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5):
            pass
    except Exception:
        pass  # non-critical, best-effort only
```

### 9. Shared Workspace

When a workflow spans multiple agents, a shared workspace directory is passed via
`message.metadata.sharedWorkspacePath`.

**Rules:**
- Read upstream artifacts from `<sharedWorkspacePath>/<upstream-agent-id>/`
- Write your outputs to `<sharedWorkspacePath>/<your-agent-id>/`
- Never write outside the shared workspace path
- Namespace your files to avoid conflicts with other agents

```python
import os
import json

workspace_path = message.get("metadata", {}).get("sharedWorkspacePath", "")

# Read upstream output
tracker_dir = os.path.join(workspace_path, "tracker-agent")
ticket_file = os.path.join(tracker_dir, "ticket.json")
if os.path.isfile(ticket_file):
    with open(ticket_file, encoding="utf-8") as fh:
        ticket = json.load(fh)

# Write your output
my_dir = os.path.join(workspace_path, "my-agent")
os.makedirs(my_dir, exist_ok=True)
with open(os.path.join(my_dir, "result.json"), "w", encoding="utf-8") as fh:
    json.dump(result, fh, ensure_ascii=False, indent=2)
```

### 10. Structured Logging

All log output MUST be prefixed with `[agent-id]` so logs can be filtered in the
Compass Web UI's agent log panel.

```python
AGENT_ID = "my-agent"

print(f"[{AGENT_ID}] Starting task {task_id}")
print(f"[{AGENT_ID}] Calling Jira MCP for ticket {ticket_key}")
print(f"[{AGENT_ID}] Task {task_id} completed with state {final_state}")
```

**Audit log** (for significant events):
```python
import json
import time

def audit_log(event, **kwargs):
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **kwargs}
    print(f"[audit] {json.dumps(entry, ensure_ascii=False)}")

audit_log("TASK_STARTED", task_id=task_id, capability="my.skill.id")
audit_log("TASK_COMPLETED", task_id=task_id, state="TASK_STATE_COMPLETED")
```

### 11. Artifact Output

Agents return results as A2A artifacts. Use `common/message_utils.py` helpers.

```python
from common.message_utils import artifact_text

# Build an artifact for the response
artifacts = [
    {
        "name": "result-summary",
        "artifactType": "text/plain",
        "parts": [{"text": "Summary of what was done."}],
        "metadata": {
            "agentId": AGENT_ID,
            "capability": "my.skill.id",
            "taskId": task_id,
        }
    }
]
```

**Rules:**
- Always include `metadata.agentId` and `metadata.capability` in every artifact.
- Include `metadata.orchestratorTaskId` (the parent Compass task ID) for traceability.
- Artifact `name` must be human-readable and unique within a task.

### 12. LLM Usage

Use `common/llm_client.py` for all LLM calls. It handles:
- OpenAI-compatible API
- Mock fallback when no LLM is configured (`ALLOW_MOCK_FALLBACK=1`)
- Proper timeout and error handling

```python
from common.llm_client import LLMClient

llm = LLMClient()

response = llm.chat(
    messages=[
        {"role": "system", "content": "You are a helpful agent."},
        {"role": "user", "content": user_text},
    ],
    max_tokens=2048,
)
```

### 13. Environment Variables (Required in .env.example)

Every agent `.env.example` MUST include:

```env
# Agent identity
AGENT_ID=my-agent
ADVERTISED_BASE_URL=http://my-agent:8080

# Registry
REGISTRY_URL=http://registry:9000

# LLM (use host.docker.internal for Docker Desktop environments)
OPENAI_BASE_URL=http://host.docker.internal:1288/v1
OPENAI_MODEL=gpt-5-mini
OPENAI_API_KEY=
ALLOW_MOCK_FALLBACK=1

# Runtime
HOST=0.0.0.0
PORT=8080
HEARTBEAT_INTERVAL=30
```

### 14. Dockerfile Requirements

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install only required system packages
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy common library first (better layer caching)
COPY common/      /app/common/
COPY my-agent/    /app/my-agent/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Label for Docker API filtering (REQUIRED)
LABEL mvp.agent_id="my-agent"
LABEL mvp.agent_name="My Agent"
LABEL mvp.agent_role="execution"

CMD ["python3", "my-agent/app.py"]
```

**Required Docker labels:**
- `mvp.agent_id` — matches `agentId` in `registry-config.json`
- `mvp.agent_name` — human-readable display name
- `mvp.agent_role` — one of: `fundamental`, `execution`, `integration`

### 15. docker-compose.yml Entry

```yaml
my-agent:
  image: mvp-my-agent:latest
  build:
    context: .
    dockerfile: my-agent/Dockerfile
  command: ["python3", "my-agent/app.py"]
  depends_on:
    registry:
      condition: service_healthy
    init-register:
      condition: service_completed_successfully
  env_file:
    - ./my-agent/.env
  environment:
    HOST: "0.0.0.0"
    PORT: "8080"
    AGENT_ID: "my-agent"
    ADVERTISED_BASE_URL: "http://my-agent:8080"
    REGISTRY_URL: "http://registry:9000"
    HEARTBEAT_INTERVAL: "30"
  labels:
    mvp.agent_id: "my-agent"
    mvp.agent_name: "My Agent"
    mvp.agent_role: "execution"
  ports:
    - "8030:8080"
  healthcheck:
    test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"]
    interval: 2s
    timeout: 2s
    retries: 20
    start_period: 2s
```

---

## Capability Naming Convention

Skill IDs follow the pattern: `<domain>.<resource>.<action>`

| Domain | Examples |
|--------|---------|
| `tracker` | `tracker.ticket.fetch`, `tracker.ticket.update`, `tracker.ticket.comment` |
| `scm` | `scm.repo.inspect`, `scm.branch.create`, `scm.pr.create` |
| `android` | `android.task.execute`, `android.build.run` |
| `ios` | `ios.task.execute` |
| `middleware` | `middleware.task.execute` |
| `team-lead` | `team-lead.task.analyze`, `team-lead.workflow.plan` |
| `review` | `review.code.check`, `review.qa.validate` |

---

## MCP Integration Pattern

MCP tools are NOT agents — they are tool servers called from within an agent.
Never register an MCP server in the Capability Registry.

**Correct pattern** (MCP called inside an agent):
```python
# Inside team-lead/app.py
# The Team Lead Agent calls Jira MCP as a tool, not as an A2A agent
def fetch_ticket_via_jira_mcp(ticket_key):
    # Call Jira MCP server
    ...
```

**Wrong pattern** (do NOT do this):
```python
# WRONG: Compass Agent calling Jira MCP directly
# Compass should only know about A2A agents, not MCP servers
```

---

## Testing Checklist for New Agents

Before submitting a new agent, verify:

- [ ] `GET /health` returns `{"status": "ok"}`
- [ ] `GET /.well-known/agent-card.json` returns valid card with `__ADVERTISED_URL__` resolved
- [ ] `POST /message:send` returns immediately (within 1 second) with a task object
- [ ] `GET /tasks/{id}` returns updated state after completion
- [ ] Agent registers itself in the Capability Registry on startup
- [ ] Heartbeat keeps the instance `idle` in the registry
- [ ] Callback URL is called on task completion (if provided in metadata)
- [ ] Logs include `[agent-id]` prefix on every line
- [ ] Artifacts include `metadata.agentId` and `metadata.orchestratorTaskId`
- [ ] Dockerfile labels include `mvp.agent_id`, `mvp.agent_name`, `mvp.agent_role`
- [ ] `.env.example` is complete and has no real credentials
- [ ] `registry-config.json` and `agent-card.json` are present

---

## Key File Locations

| Purpose | Path |
|---------|------|
| Compass Agent (control plane) | `compass/app.py` |
| Capability Registry | `registry/app.py` |
| Shared LLM client | `common/llm_client.py` |
| Shared Registry client | `common/registry_client.py` |
| Task state machine | `common/task_store.py` |
| Artifact storage | `common/artifact_store.py` |
| Docker launcher | `common/launcher.py` |
| Instance heartbeat | `common/instance_reporter.py` |
| A2A message helpers | `common/message_utils.py` |
| Registry bootstrap | `scripts/init_register.py` |
| E2E tests | `tests/test_e2e.py` |
