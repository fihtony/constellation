# Constellation — GitHub Copilot Instructions

## Language Policy

| Content type | Language |
|---|---|
| Explanations, design discussions, answers to questions | **Chinese** |
| Design documents (`docs/*.md`) | **Chinese** |
| Source code, code comments, scripts, tests, skills, instruction files, `README.md` | **English** |

All newly added code strings, comments, scripts, tests, skills, and instruction text must remain in English. User-facing explanations in chat stay in Chinese.

---

## Change Workflow Checklist

After **any** code change, always consider these three questions before closing the task:

1. **Design document** — Does the change affect architecture, data flow, agent capabilities, or API contracts? If yes, update `docs/constellation-system-design-zh.md` in Chinese.
2. **Skills** — Does the change introduce, modify, or remove an agent workflow, API pattern, or domain-specific procedure? If yes, create or update the relevant `.github/skills/<name>/SKILL.md` file.
3. **Copilot instructions** — Does the change affect agent names, label conventions, network names, ports, directory structure, or shared patterns? If yes, update this file.

---

## Project Overview

**Constellation** is a multi-agent system built on the A2A (Agent-to-Agent) protocol.
The system is named after the idea that each agent is a "star" — independent but connected,
working together to complete complex engineering tasks.

### Core Components

| Component | Directory | Role |
|-----------|-----------|------|
| **Compass Agent** | `compass/` | Control-plane entry point. Routes ALL tasks to Team Lead, manages workflow, owns the Web UI. |
| **Team Lead Agent** | `team-lead/` | Intelligence layer. Analyzes tasks, gathers Jira/design context, plans, dispatches to dev agents, reviews output, and summarizes results. Runs on port 8030. |
| **Capability Registry** | `registry/` | Agent discovery and instance tracking. All agents register here on startup. |
| **Jira Agent** | `jira/` | Integrates with Jira-compatible systems. Fetches tickets, updates status, posts comments. Runs on port 8010 (Docker service name: `jira`). |
| **SCM Agent** | `scm/` | Integrates with Git SCM (Bitbucket/GitHub). Repo inspection, branch, PR operations. Runs on port 8020. |
| **Android Agent** | `android/` | On-demand execution agent. Launched per-task by Team Lead via Docker socket. |
| **UI Design Agent** | `ui-design/` | Design context agent. Fetches design data from Figma (REST API) and Google Stitch (MCP). Runs on port 8040. |
| **Office Agent** | `office/` | On-demand document agent for local office files. Summarizes, analyzes, and organizes user-authorized folders. Runs on port 8060. |
| **Common Library** | `common/` | Shared modules: registry client, launcher, runtime adapters, rules loader, artifact store, task store, etc. |

### MCP Tool Integrations (replacing standalone agents)

The system uses MCP (Model Context Protocol) servers instead of dedicated agents for:
- **Jira MCP** — replaces the old Jira Agent. Used by the Team Lead Agent (NOT directly by Compass).
- **GitHub MCP** — replaces the old Bitbucket Agent. Used by execution agents.
- **Google Stitch MCP** — provides design context. Used by execution agents.

### Architecture: Compass + Team Lead Pattern

```
User
  └─► Compass Agent (control plane, thin router, UI)
    ├─► Office Agent (per-task container: office.document.summarize / office.data.analyze / office.folder.organize)
    └─► Team Lead Agent (intelligence layer — analysis, planning, coordination, review)
      ├─► Jira Agent (A2A: jira.ticket.fetch, jira.comment.add, …)
      ├─► UI Design Agent (A2A: figma.page.fetch, stitch.screen.fetch, …)
      ├─► Android Agent (per-task container: android.task.execute)
      ├─► iOS Agent (per-task container, future)
      └─► Web Agent (per-task container, future)
```

**Design Rationale**:
- Compass Agent uses the shared agentic runtime for all user-facing routing work: task classification, clarification handling, and final user summaries. It routes office/document tasks directly to Office Agent and development tasks to Team Lead.
- Team Lead Agent is the intelligence layer responsible for: task analysis, info gathering (Jira, design), planning, dev agent dispatch, code review, and result summarization.
- Team Lead owns architecture decisions, delivery planning, review, and Jira audit comments; it does NOT implement product code itself.
- Office Agent is a per-task execution agent for user-authorized local files. It may summarize, analyze, or reorganize documents, but only within explicitly mounted paths and with output mode chosen by the user.
- Team Lead handles INPUT_REQUIRED by pausing its workflow and waiting for user input forwarded by Compass. No new Team Lead instance is created for resume — the SAME instance resumes.
- Dev agents (android, ios, web) are launched per-task by Team Lead via Docker socket + Registry + Launcher.

### Container Runtime

This project uses **Docker Desktop by default**, and also supports **Rancher Desktop**.
- `CONTAINER_RUNTIME=docker` is the default; set `CONTAINER_RUNTIME=rancher` to use Rancher Desktop.
- Host machine alias from containers:
  - Docker Desktop: `host.docker.internal`
  - Rancher Desktop: `host.rancher-desktop.internal`
- Socket path visible to the current process:
  - Host process + Docker Desktop: `/var/run/docker.sock`
  - Host process + Rancher Desktop: `~/.rd/docker.sock` (or override with `DOCKER_SOCKET`)
  - Inside launcher containers: always `/var/run/docker.sock`
- Persistent launcher services must mount the host socket to `/var/run/docker.sock` inside the container.
- Nested per-task launchers must re-bind the socket using the host-side mount source discovered from the current container, not by reusing the current container path as a host source.
- Network name: `constellation-network`
- If `OPENAI_BASE_URL` is unset, Copilot Connect resolves automatically:
  - host process: `http://localhost:1288/v1`
  - Docker container: `http://host.docker.internal:1288/v1`
  - Rancher container: `http://host.rancher-desktop.internal:1288/v1`

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
      "orchestratorCallbackUrl": "http://<orchestrator-service>/tasks/{id}/callbacks",
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
- Skill IDs follow dot-notation: `<domain>.<resource>.<action>` (e.g., `jira.ticket.fetch`).

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
- `"persistent"` — agent is always running (jira, scm, compass). Registered once at startup.
- `"per-task"` — agent is launched on demand by Compass via Docker socket (android, ios). Must include `launchSpec`.

**For `per-task` agents, add `launchSpec`:**
```json
{
  "agentId": "android-agent",
  "executionMode": "per-task",
  "launchSpec": {
    "image": "constellation-android-agent:latest",
    "platform": "linux/amd64",
    "port": 8000
  },
  "scalingPolicy": {
    "maxInstances": 3,
    "perInstanceConcurrency": 1,
    "idleTimeoutSeconds": 120
  }
}
```

`launchSpec.platform` is optional. Use it when a per-task agent depends on architecture-specific toolchains or binaries. Current example: the Android agent pins `linux/amd64` so Android SDK / AAPT2 tooling runs correctly on Apple Silicon hosts through Docker or Rancher Desktop.

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

Agents SHOULD report major workflow steps to the orchestrator via the progress endpoint.
Prefer deriving the base service URL from `message.metadata.orchestratorCallbackUrl`; if that is unavailable, discover `orchestrator.progress.report` through Registry. Do not hardcode `COMPASS_URL` in child agents.
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
- **A2A boundary rule**: An agent may only read workspace files written by agents it directly orchestrates. Compass may read `team-lead/` files (Team Lead is Compass's downstream). Team Lead may read `android-agent/`, `web-agent/` files (they are Team Lead's downstream). **Compass must NOT read execution-agent files** (android-agent, web-agent, etc.) directly — all evidence must reach Compass via Team Lead's A2A callback artifacts.
- **Evidence propagation**: Execution agents must include key result fields (`prUrl`, `branch`, `jiraInReview`) in their A2A artifact **metadata** so that Compass can receive them through Team Lead's final callback without scanning the filesystem.

```python
import os
import json

workspace_path = message.get("metadata", {}).get("sharedWorkspacePath", "")

# Read upstream output
jira_dir = os.path.join(workspace_path, "jira-agent")
ticket_file = os.path.join(jira_dir, "ticket.json")
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

Use `common/runtime/adapter.py` for all agentic LLM/CLI calls. It handles:
- `copilot-cli` as the shared default runtime backend for LLM-enabled agents
- `claude-code` as an optional compatible backend
- `connect-agent` as an explicit transport-backed backend when a workflow selects it
- fail-fast runtime errors when the configured backend is unavailable or misconfigured
- Proper timeout and structured result handling

`common/runtime/copilot_connect.py` is a compatibility single-shot wrapper over the Copilot Connect transport. It is not a selectable agentic runtime backend.

```python
from common.runtime.adapter import get_runtime

runtime = get_runtime()
result = runtime.run(
  prompt=user_text,
  system_prompt="You are a helpful agent.",
  max_tokens=2048,
)
response_text = result["raw_response"] or result["summary"]
```

### 13. Environment Variables (Required in .env.example)

Every agent `.env.example` MUST include:

```env
# Agent identity
AGENT_ID=my-agent
ADVERTISED_BASE_URL=http://my-agent:8080

# Registry
REGISTRY_URL=http://registry:9000

# LLM
# Leave OPENAI_BASE_URL unset to use runtime-aware defaults:
#   localhost on host, host.docker.internal in Docker,
#   host.rancher-desktop.internal in Rancher
# OPENAI_BASE_URL=
OPENAI_MODEL=gpt-5.4-mini
OPENAI_API_KEY=

# Runtime
HOST=0.0.0.0
PORT=8080
HEARTBEAT_INTERVAL=30
```

### 14. LLM Prompt Files

**MANDATORY**: Every agent that calls an LLM MUST keep all prompt strings in a dedicated
`prompts.py` file in the agent's root directory (same level as `app.py`).

**Rules:**
- File name: `prompts.py` (fixed, no exceptions)
- `app.py` MUST NOT contain any inline LLM prompt strings
- Naming convention: `<PURPOSE>_SYSTEM` for system prompts, `<PURPOSE>_TEMPLATE` for user prompt templates
- Template variables use Python f-string format: `{variable_name}`

**Rationale:** Prompts are core business logic. Keeping them separate enables version tracking,
A/B testing, and code review without mixing prompt changes with logic changes.

```
my-agent/
├── app.py        ← logic only, no prompt strings
├── prompts.py    ← ALL LLM prompt strings
└── agent-card.json
```

**Example `prompts.py`:**
```python
ANALYZE_SYSTEM = "You are a ..."
ANALYZE_TEMPLATE = "Given the request: {user_text}\nRespond with JSON: ..."
```

**Usage in `app.py`:**
```python
from my_agent import prompts
response = generate_text(
    prompts.ANALYZE_TEMPLATE.format(user_text=user_text),
    "[my-agent] analyze",
    system_prompt=prompts.ANALYZE_SYSTEM,
)
```

When an agent needs curated workspace playbooks (for example delivery guidance for architecture/frontend/backend/database), build the final system prompt through `common.rules_loader.build_system_prompt(...)` and pass `skill_names=[...]` rather than open-coding repeated file reads in each call site.

If an agent reads workspace skills at runtime, its Dockerfile MUST also copy `.github/skills/` into the image (current convention: `/app/.github/skills/`).

### 15. Dockerfile Requirements

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
LABEL constellation.agent_id="my-agent"
LABEL constellation.agent_name="My Agent"
LABEL constellation.agent_role="execution"

# Run as non-root user (REQUIRED for security — OWASP A05).
# Do this after all installs and file copies so permissions are set correctly.
RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python3", "my-agent/app.py"]
```

**Required Docker labels:**
- `constellation.agent_id` — matches `agentId` in `registry-config.json`
- `constellation.agent_name` — human-readable display name
- `constellation.agent_role` — one of: `fundamental`, `execution`, `integration`

**Non-root user requirement:**
Every agent container MUST run as a non-root user (`appuser`, UID 1000). The `USER appuser` instruction must come after all `RUN`/`COPY` steps so that `/app` ownership is set correctly.
The Compass container must add the mounted Docker socket's numeric group id via `group_add` in `docker-compose.yml` so the non-root user can access the socket on both Docker Desktop and Rancher Desktop. Keep `0` for Docker Desktop compatibility and use `DOCKER_SOCKET_GID` to override the Rancher/Desktop-in-container gid when needed. On macOS, derive that value from a short helper container, for example: `export DOCKER_SOCKET_GID=$(docker run --rm -v "${DOCKER_SOCKET:-$HOME/.rd/docker.sock}:/var/run/docker.sock" python:3.12-slim python -c "import os; print(os.stat('/var/run/docker.sock').st_gid)")`.

### 16. docker-compose.yml Entry

```yaml
my-agent:
  image: constellation-my-agent:latest
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
    constellation.agent_id: "my-agent"
    constellation.agent_name: "My Agent"
    constellation.agent_role: "execution"
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
| `jira` | `jira.ticket.fetch`, `jira.ticket.update`, `jira.ticket.comment` |
| `scm` | `scm.repo.inspect`, `scm.branch.create`, `scm.pr.create` |
| `android` | `android.task.execute`, `android.build.run` |
| `ios` | `ios.task.execute` |
| `middleware` | `middleware.task.execute` |
| `office` | `office.document.summarize`, `office.data.analyze`, `office.folder.organize` |
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
- [ ] Dockerfile labels include `constellation.agent_id`, `constellation.agent_name`, `constellation.agent_role`
- [ ] `.env.example` is complete with REQUIRED/OPTIONAL annotations and no real credentials
- [ ] `registry-config.json` and `agent-card.json` are present

---

## Key File Locations

| Purpose | Path |
|---------|------|
| **Team Lead Agent** | `team-lead/` | Intelligence layer: analysis, planning, dispatch, review | port 8030 |
| Team Lead prompts | `team-lead/prompts/system/` and `team-lead/prompts/tasks/` | Modular system/task prompt assets for Team Lead |
| Jira prompts | `jira/prompts.py` | ALL LLM prompt strings for Jira Agent |
| SCM prompts | `scm/prompts.py` | ALL LLM prompt strings for SCM Agent |
| UI Design prompts | `ui-design/prompts.py` | ALL LLM prompt strings for UI Design Agent |
| **Android Agent** | `android/` | Per-task Android execution agent | port 8000 |
| Android prompts | `android/prompts.py` | ALL LLM prompt strings for Android Agent |
| UI Design Agent | `ui-design/` | Figma REST API + Google Stitch MCP | port 8040 |
| Office Agent | `office/` | Local office document execution agent | port 8060 |
| Office prompts | `office/prompts.py` | ALL LLM prompt strings for Office Agent |
| Office document tools | `office/tools/document_tools.py` | Agent-local document readers (read_pdf, read_docx, etc.), NOT in `common/` |
| UI Design client (Figma) | `ui-design/figma_client.py` | Agent-local, NOT in `common/` |
| UI Design client (Stitch) | `ui-design/stitch_client.py` | Agent-local, NOT in `common/` |
| Compass Agent (control plane) | `compass/app.py` |
| Compass office routing helpers | `compass/office_routing.py` | Path validation and Docker bind-mount helpers for office tasks (state machine removed; LLM uses `validate_office_paths` control tool instead) |
| Compass completeness gate helpers | `compass/completeness.py` | PR evidence extraction, completeness checks, follow-up message builder, task card status derivation |
| Team Lead workflow helpers | `team-lead/agentic_workflow.py` | Runtime tool list, task prompt builder, input-wait helper, and control tool wiring |
| Runtime adapter factory | `common/runtime/adapter.py` | Unified runtime contract + backend factory |
| Shared runtime env template | `common/.env.example` | Shared default runtime/timezone config loaded before agent-local `.env` |
| Local time helpers | `common/time_utils.py` | Shared local timestamp helpers for workspace and audit logs |
| Workspace/debug log helpers | `common/devlog.py` | Shared debug log + workspace stage logging helpers |
| Copilot CLI backend | `common/runtime/copilot_cli.py` | Primary agentic CLI backend (supports `run_agentic()` via ReAct text loop) |
| Claude Code backend | `common/runtime/claude_code.py` | Optional compatible backend |
| Copilot Connect transport wrapper | `common/runtime/copilot_connect.py` | Single-shot compatibility wrapper over the LLM transport used by `connect-agent`; not a selectable runtime backend |
| Capability Registry | `registry/app.py` |
| Shared low-level LLM client | `common/llm_client.py` | Legacy OpenAI-compatible helper used by runtime internals |
| Shared Registry client | `common/registry_client.py` |
| Task state machine | `common/task_store.py` |
| Artifact storage | `common/artifact_store.py` |
| Docker launcher | `common/launcher.py` |
| Rancher launcher | `common/launcher_rancher.py` |
| Instance heartbeat | `common/instance_reporter.py` |
| A2A message helpers | `common/message_utils.py` |
| Registry bootstrap | `scripts/init_register.py` |
| E2E tests | `tests/test_e2e.py` |

## Shared Runtime Notes

- LLM-enabled agents (`team-lead`, `web`, `android`, `jira`, `scm`, `ui-design`, `office`) should load shared defaults from `common/.env` first, then apply their local `.env` overrides.
- Shared runtime defaults live in `common/.env`, including `AGENT_RUNTIME`, model selection, and `CONTAINER_RUNTIME`. Team Lead, Web, and Android should inherit that baseline unless a future design change explicitly requires a per-task override in `registry-config.json > launchSpec.env`.
- Protected GitHub/SCM credential variables (`GH_TOKEN`, `GITHUB_TOKEN`, `COPILOT_GITHUB_TOKEN`, `SCM_TOKEN`, `SCM_USERNAME`, `SCM_PASSWORD`, `TEST_GITHUB_TOKEN`) are file-backed by default. Ambient host values must be ignored unless a launcher or test has already loaded its own `.env` and explicitly marks the child process with `CONSTELLATION_TRUSTED_ENV=1`.
- Runtime Git commands must use the isolated helper environment from `common.env_utils.build_isolated_git_env()` so agent subprocesses never read host Git credential helpers, host keychains, or user-level `~/.gitconfig`.
- `copilot-cli` runtime authentication is isolated as well: only `COPILOT_GITHUB_TOKEN` is supported for agent execution. Do not rely on `GH_TOKEN`, `GITHUB_TOKEN`, `gh auth`, or system keychain fallbacks inside agents.
- Launchers and integration tests must sanitize inherited host GitHub credentials before spawning subprocesses. Test scripts may use only file-backed values from `tests/.env` for GitHub auth.
- Any test that needs a Jira ticket URL/key, GitHub/Bitbucket repo URL, Figma URL, or Stitch URL must source that target from `tests/.env` (directly or through a helper that reads `tests/.env`). Do not hardcode real ticket IDs, repo URLs, or design URLs in test scripts.
- `ARTIFACT_ROOT` is the only artifact-root config now. Launchers must discover the host-side bind source for `/app/artifacts` by inspecting the current container's mounts through the Docker-compatible API; do not re-introduce `ARTIFACT_ROOT_HOST`.
- `registry` remains a non-agentic control-plane service. `compass` is now an agentic control-plane service for routing, clarification interpretation, and user-facing final summaries, but it must still avoid unbounded external-system reasoning loops and must not bypass registered boundary agents.
- Task workspaces should keep `command-log.txt` and `stage-summary.json` under each agent subdirectory for auditability; runtime details belong inside `stage-summary.json` as `runtimeConfig`, not in a separate `runtime-config.json` file.
- The shared `connect-agent` runtime prompt must stay domain-neutral. Task-specific development, design-to-code, office, or audit rules belong in the caller's agent prompt or explicit system prompt override, not in the runtime default prompt.
- Web Agent and Android Agent now use `runtime.run_agentic()` for the real repository implementation phase. Keep `run()` only for bounded single-shot subtasks such as structured analysis, planning, self-assessment, or summarization.
- When Team Lead has already fetched Jira or UI-design context, it must pass bounded copies to the downstream dev agent through A2A metadata (`jiraContext`, `designContext`). Dev agents must consume that handed-off context first and only call Jira / UI Design again when they need additional detail beyond the provided payload.
- Team Lead and Web currently inject the **six** workspace delivery playbooks `constellation-architecture-delivery`, `constellation-frontend-delivery`, `constellation-backend-delivery`, `constellation-database-delivery`, `constellation-code-review-delivery`, and `constellation-testing-delivery` through `build_system_prompt(...)`; their `stage-summary.json` should therefore retain both `runtimeConfig.runtime` and `runtimeConfig.skillPlaybooks` for auditability.
- `compass` and `team-lead` prompt manifests should also inject `constellation-generic-agent-workflow` ahead of agent-specific guidance, and new prompts/tests should prefer the canonical local workspace tool names `read_local_file`, `write_local_file`, `edit_local_file`, `list_local_dir`, and `search_local_files`. Legacy aliases (`read_file`, `write_file`, `glob`, `grep`) remain compatibility-only and should not be treated as the primary contract in new runtime-first work.
- In execution task workspaces, generated source files should live in the real cloned repository directory; `web-agent/` and similar agent subdirectories are for metadata and audit artifacts only.
- For repo-backed development tasks, Team Lead must instruct the dev agent to clone the target repository via the SCM agent into the shared workspace before editing files, and Web Agent must fail fast if a repo URL is present but no shared workspace is available or the clone path escapes that workspace.
- Web Agent branches should use deterministic naming based on Jira key plus orchestrator task id when available; only docs/tests-only changes may use `chore/...` naming without a ticket key.
- Team Lead review for repo-backed tasks must require clone/branch/PR evidence and should post audit-ready rejection comments to Jira when a delivery is rejected.
- Boundary agents (Jira, SCM, UI Design, future Jenkins/Stitch-style integrations) must be discovered through Registry capabilities at runtime; do not hardcode their service URLs inside Team Lead or execution agents.
- Common boundary-tool wrappers must send the standard A2A `message:send` envelope (`message` at the top level plus `configuration.returnImmediately`) and, when the caller expects a synchronous result, poll `/tasks/{id}` until a terminal state before returning. Do not introduce new JSON-RPC-style wrapper envelopes for agent-to-agent tooling.
- Compass now attaches a task permission snapshot in `message.metadata.permissions` for routed task work. Boundary agents must enforce that snapshot themselves instead of trusting upstream prompt discipline. All agent-to-agent calls must carry permissions through A2A `message.metadata.permissions`; execution agents and Team Lead must not fall back to direct HTTP headers/body fields when calling boundary agents. Direct HTTP convenience endpoints, if retained, are for operator/manual or test-only access paths and must not be the normal inter-agent transport.
- Development-task SCM protected branches are defined centrally by `common/permissions/development.json > scopeConfig.scm.protectedBranchPatterns` as full regex matches. Default protected branches are `main`, `master`, `develop`, and `release/*`; any other branch name is treated as a development branch unless policy overrides it.
- The permission system is pre-release and fail-closed by default. In `PERMISSION_ENFORCEMENT=strict`, missing or malformed permission snapshots must reject both read and write boundary operations; do not add compatibility fallbacks that weaken enforcement.
- Team Lead intake/gathering should use the agentic runtime to emit structured pending actions, but the code must still execute boundary calls itself through Registry-discovered capabilities. Do not let runtime output bypass A2A boundaries or directly hardcode external system access.
- Repo-backed development agents must work inside the shared-workspace clone on a local development branch, run local build/test validation before PR creation, and persist branch/test/PR evidence in their agent workspace. For UI tasks with design context, they must also capture design-reference plus implementation screenshots and include PR-safe copies in `docs/evidence/` when the repo workflow allows it.
- Architecture-sensitive per-task agents may declare `launchSpec.platform`; `common/launcher.py` and `common/launcher_rancher.py` now pass that through to the Docker create payload. Android currently uses `linux/amd64` because some Android SDK binaries remain x86_64-only in this environment.
- Android Agent now performs a bounded local build/test recovery loop before PR creation: on Gradle/unit-test failure it re-runs validation with stable CI-friendly Gradle flags, asks the runtime for targeted file fixes, reapplies those fixes in the clone, reruns validation, and persists every attempt in `android-agent/test-results.json`.
- The validated Android container profile uses `--max-workers=1`, `-Pkotlin.compiler.execution.strategy=in-process`, `-Dkotlin.daemon.enabled=false`, `ANDROID_GRADLE_JVM_ARGS=-Xmx2g -Dfile.encoding=UTF-8`, and `android.dexBuilderWorkerCount=1` written to `GRADLE_USER_HOME/gradle.properties` before each build. The agent also clears stale Gradle journal lock files (`caches/journal-1/journal-1.lock`) before each build invocation to prevent failures from previously killed containers. The Android per-task container is launched with `memory: "4g"` (see `android/registry-config.json`). Kotlin 2.0 + Compose IR compilation requires at least 2 GB of JVM heap; the host VM (Rancher Desktop / Docker Desktop) must be configured with ≥6 GB RAM for cold builds. Cold builds with cached Gradle artifacts pass with less memory.
- Always construct `RegistryClient(REGISTRY_URL)` explicitly and pass it to `AgentDirectory(owner_id, registry_client)`. Never rely on the module-level `REGISTRY_URL` default inside `RegistryClient` — `load_dotenv` may not have run yet at import time.
- Registry now exposes topology metadata (`/topology`, `/events?sinceVersion=`); agents that call other agents should cache capability lookups and refresh on cache miss or topology change.
- Compass applies a final completeness gate to Team Lead results **using only A2A artifacts from Team Lead's callback** — it must never scan execution-agent subdirectories in the shared workspace (e.g., `android-agent/pr-evidence.json`, `web-agent/jira-actions.json`). Those files are internal to the Team Lead ↔ dev-agent pipeline. Compass reads PR URL and branch from artifact metadata (`prUrl`, `branch`), and Jira "In Review" status from the `jiraInReview` boolean flag in the execution agent's artifact metadata. Compass may still read `team-lead/` workspace files (Team Lead's own output) for display and fallback purposes. Compass may trigger a same-workspace follow-up cycle before marking the user task complete. The only exception is an explicit Team Lead validation checkpoint artifact (`metadata.validationCheckpoint=true`), which intentionally stops before dev dispatch and skips the completeness gate.
- **Per-task agent exit rule** (implemented in `common/per_task_exit.py`):
  - The parent agent embeds `"exitRule": {"type": "wait_for_parent_ack", "ack_timeout_seconds": 300}` in the child's message metadata.
  - The child agent calls `PerTaskExitHandler.parse(metadata)` to read the rule, and calls `exit_handler.apply(task_id, rule, shutdown_fn=_schedule_shutdown)` in its workflow `finally` block.
  - The parent sends `POST {child_service_url}/tasks/{task_id}/ack` when it is done with the child (all review cycles complete, callback processed).
  - The child exposes `POST /tasks/{id}/ack` to receive the parent ACK.
  - Supported rule types: `wait_for_parent_ack` (default), `immediate` (old AUTO_STOP behavior), `persistent` (no auto-stop).
  - Default ACK timeout: 5 minutes. The child shuts down after the timeout even if no ACK arrives.
  - For revisions: Team Lead reuses the **same** dev-agent container (same service URL, new task ID via `POST /message:send`). It does NOT launch a new container. The ACK is sent only after all review cycles are done.
  - Compass ACKs Team Lead after the completeness gate passes (or max revisions reached).
- Python virtual environments created by Web Agent are placed in `tempfile.gettempdir()/constellation-venv-{hash}`, NOT inside the cloned repo directory, to avoid Docker-path shebang issues when the workspace is accessed locally.
- Use `LOCAL_TIMEZONE` (preferred) or `TZ` to keep workspace timestamps aligned with the operator's local time.
- Agent-specific tools must live in the agent directory (e.g. `office/tools/document_tools.py`), NOT in `common/tools/`. Each agent loads its own tools before calling `runtime.run_agentic(tools=TOOL_NAMES)`. The runtime adapter does NOT hardcode domain tool imports — it only loads core shared tools (coding, planning, control, registry, validation). Domain tools (jira, scm, design, document readers) are loaded by the agent that needs them.
- All LLM-enabled agents MUST use runtime-specific Dockerfiles (e.g. `Dockerfile.connect-agent`, `Dockerfile.copilot-cli`, `Dockerfile.claude-code`). Generic `Dockerfile` files are forbidden for agents that have runtime variants. Only infrastructure services without runtime variants (registry, im-gateway) may use a plain `Dockerfile`.
- Compass routes office tasks via Registry capability lookup, not by hardcoding the Office Agent URL. The `dispatch_agent_task` tool discovers and launches per-task agents automatically through Registry + Launcher.
- Office Agent completes a delivery review cycle with Compass: Compass validates output completeness via `aggregate_task_card`, may send revision comments back, and only ACKs the Office Agent after the delivery is accepted (or max revisions exhausted).

---

## v2 Redesign Architecture (framework/ and agents/)

The v2 codebase lives in `framework/`, `agents/`, `skills/`, `config/`, `scripts/`, and `tests/`. It coexists with v1 code under the repository root (`compass/`, `team-lead/`, `common/`, `registry/`, etc.). v1 is NOT modified.

### Core Design Principle: Graph outside, ReAct inside

This is the fundamental architecture decision. Every agent implementation must follow it.

**Graph (workflow) handles macro lifecycle** — deterministic state transitions, branching, looping (review cycles), interrupt/resume, and checkpoint recovery. Defined declaratively via `Workflow(edges=[...])`.

**ReAct (LLM) handles micro decisions within nodes** — reasoning, tool selection, context interpretation, code generation. Used via `runtime.run()` (single-shot) or `runtime.run_agentic()` (multi-turn tool loop).

#### When to use Graph (workflow)

- The agent has **identifiable lifecycle stages** (analyze → plan → execute → review → report)
- State transitions are **deterministic** — the next step depends on the current step's output, not on open-ended reasoning
- The workflow includes **loops** (e.g., review → revision → re-review)
- The workflow needs **interrupt/resume** for human-in-the-loop
- You need **checkpoint persistence** for crash recovery

#### When to use ReAct (LLM-driven)

- The task is **open-ended** — the agent doesn't know the exact steps in advance
- User requests are **diverse and unpredictable** (e.g., Compass receiving arbitrary user messages)
- The agent needs to **choose tools dynamically** based on context
- A single node within a graph needs to reason about which sub-tools to call

#### Agent Classification (fixed)

| Agent | Orchestration | Rationale |
|-------|--------------|-----------|
| Compass | **ReAct-first** (`workflow=None`) | Open-ended user entry point, diverse request types |
| Team Lead | **Graph-first** | Deterministic stages: receive → analyze → gather → plan → dispatch → review → report |
| Web Dev | **Graph-first** | Deterministic stages: setup → analyze → implement → test → fix → PR → report |
| Code Review | **Graph-first** | Deterministic stages: load PR → quality → security → tests → requirements → report |
| Jira / SCM / UI Design | **Direct adapter** | Controlled API proxy, no orchestration needed |

### TaskStore Requirement

Every agent MUST use `TaskStore` for task lifecycle management. The `AgentServices` dataclass includes a `task_store` field.

**Rules:**
- Create tasks via `task_store.create_task(agent_id=...)` — NOT by constructing `Task()` objects directly
- Return real task state from `get_task()` via `task_store.get_task_dict(task_id)`
- Mark completion via `task_store.complete_task(task_id, artifacts, message)`
- Mark failure via `task_store.fail_task(task_id, error)`
- Available backends: `InMemoryTaskStore` (dev/test), `SqliteTaskStore` (production MVP)

### Callback Requirement

Graph-first agents (Team Lead, Web Dev, Code Review) MUST send A2A callbacks on completion:

```python
callback_url = message.get("metadata", {}).get("orchestratorCallbackUrl", "")
if callback_url:
    _send_callback(callback_url, task.id, result, agent_id)
```

### v2 Directory Structure

```
constellation/
├── framework/           # Shared framework (workflow, session, events, skills, plugins, TaskStore)
│   ├── agent.py         # BaseAgent, AgentServices, AgentDefinition
│   ├── workflow.py      # Workflow, CompiledWorkflow, WorkflowRunner, interrupt
│   ├── task_store.py    # TaskStore (InMemory + SQLite)
│   ├── session.py       # SessionService (InMemory + SQLite)
│   ├── event_store.py   # EventStore (InMemory + SQLite)
│   ├── checkpoint.py    # CheckpointService (InMemory + SQLite)
│   ├── skills.py        # SkillsRegistry
│   ├── plugin.py        # PluginManager
│   ├── permissions.py   # PermissionEngine
│   ├── a2a/             # A2A protocol types, HTTP server, client
│   ├── runtime/         # Multi-backend runtime adapter (connect-agent, copilot-cli, etc.)
│   └── tools/           # BaseTool, ToolRegistry
├── agents/              # All v2 agent implementations
│   ├── compass/         # ReAct-first control plane
│   ├── team_lead/       # Graph-first intelligence layer
│   ├── web_dev/         # Graph-first dev execution
│   ├── code_review/     # Graph-first review pipeline
│   ├── jira/            # Direct adapter
│   ├── scm/             # Direct adapter
│   └── ui_design/       # Direct adapter (Figma + Stitch)
├── skills/              # Skill definitions (skill.yaml + instructions.md)
├── config/              # Global config (constellation.yaml, permissions/)
├── scripts/             # Launch scripts
└── tests/               # Unit (126+), integration, E2E
```

### v2 Testing

```bash
# Run all unit tests (no external dependencies)
source .venv/bin/activate && python -m pytest tests/unit/ -v

# Run integration tests (needs API credentials in tests/.env)
python -m pytest tests/integration/ -m live -v

# Run E2E tests (mock scenarios)
python -m pytest tests/e2e/ -v
```
