# Constellation — GitHub Copilot Instructions

## Language Policy

| Content type | Language |
|---|---|
| Explanations, design discussions, answers to questions | **Chinese (中文)** |
| Design documents (`docs/*.md`) | **Chinese (中文)** |
| Source code, comments, tests, `README.md` | **English** |

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
| **Tracker Agent** | `jira/` | Integrates with Jira-compatible systems. Fetches tickets, updates status, posts comments. Runs on port 8010 (Docker service name: `tracker`). |
| **SCM Agent** | `scm/` | Integrates with Git SCM (Bitbucket/GitHub). Repo inspection, branch, PR operations. Runs on port 8020. |
| **Android Agent** | `android/` | On-demand execution agent. Launched per-task by Team Lead via Docker socket. |
| **UI Design Agent** | `ui-design/` | Design context agent. Fetches design data from Figma (REST API) and Google Stitch (MCP). Runs on port 8040. |
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
         └─► Team Lead Agent (intelligence layer — analysis, planning, coordination, review)
                ├─► Jira Agent (A2A: jira.ticket.fetch, jira.comment.add, …)
                ├─► UI Design Agent (A2A: figma.page.fetch, stitch.screen.fetch, …)
                ├─► Android Agent (per-task container: android.task.execute)
                ├─► iOS Agent (per-task container, future)
                └─► Web Agent (per-task container, future)
```

**Design Rationale**:
- Compass Agent routes ALL user tasks to Team Lead (`team-lead.task.analyze`). It does NOT infer workflow or call Jira/SCM/design agents directly.
- Team Lead Agent is the intelligence layer responsible for: task analysis, info gathering (Jira, design), planning, dev agent dispatch, code review, and result summarization.
- Team Lead handles INPUT_REQUIRED by pausing its workflow and waiting for user input forwarded by Compass. No new Team Lead instance is created for resume — the SAME instance resumes.
- Dev agents (android, ios, web) are launched per-task by Team Lead via Docker socket + Registry + Launcher.

### Container Runtime

This project uses **Docker Desktop by default**, and also supports **Rancher Desktop**.
- `CONTAINER_RUNTIME=docker` is the default; set `CONTAINER_RUNTIME=rancher` to use Rancher Desktop.
- Host machine alias from containers:
  - Docker Desktop: `host.docker.internal`
  - Rancher Desktop: `host.rancher-desktop.internal`
- Container socket default:
  - Docker Desktop: `/var/run/docker.sock`
  - Rancher Desktop: `~/.rd/docker.sock` (or override with `DOCKER_SOCKET`)
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
    "image": "constellation-android-agent:latest",
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

Use `common/runtime/adapter.py` for all agentic LLM/CLI calls. It handles:
- `copilot-cli` as the primary production backend
- `claude-code` as an optional compatible backend
- `copilot-connect` as the OpenAI-compatible fallback / local integration backend
- Mock fallback when no real backend is available (`ALLOW_MOCK_FALLBACK=1`)
- Proper timeout and structured result handling

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
OPENAI_MODEL=gpt-5-mini
OPENAI_API_KEY=
ALLOW_MOCK_FALLBACK=1

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

CMD ["python3", "my-agent/app.py"]
```

**Required Docker labels:**
- `constellation.agent_id` — matches `agentId` in `registry-config.json`
- `constellation.agent_name` — human-readable display name
- `constellation.agent_role` — one of: `fundamental`, `execution`, `integration`

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
- [ ] Dockerfile labels include `constellation.agent_id`, `constellation.agent_name`, `constellation.agent_role`
- [ ] `.env.example` is complete with REQUIRED/OPTIONAL annotations and no real credentials
- [ ] `registry-config.json` and `agent-card.json` are present

---

## Key File Locations

| Purpose | Path |
|---------|------|
| **Team Lead Agent** | `team-lead/` | Intelligence layer: analysis, planning, dispatch, review | port 8030 |
| Team Lead prompts | `team-lead/prompts.py` | ALL LLM prompt strings for Team Lead |
| Jira prompts | `jira/prompts.py` | ALL LLM prompt strings for Jira Agent |
| SCM prompts | `scm/prompts.py` | ALL LLM prompt strings for SCM Agent |
| UI Design prompts | `ui-design/prompts.py` | ALL LLM prompt strings for UI Design Agent |
| UI Design Agent | `ui-design/` | Figma REST API + Google Stitch MCP | port 8040 |
| UI Design client (Figma) | `ui-design/figma_client.py` | Agent-local, NOT in `common/` |
| UI Design client (Stitch) | `ui-design/stitch_client.py` | Agent-local, NOT in `common/` |
| Compass Agent (control plane) | `compass/app.py` |
| Runtime adapter factory | `common/runtime/adapter.py` | Unified runtime contract + backend factory |
| Shared runtime env template | `common/.env.example` | Shared default runtime/timezone config loaded before agent-local `.env` |
| Local time helpers | `common/time_utils.py` | Shared local timestamp helpers for workspace and audit logs |
| Workspace/debug log helpers | `common/devlog.py` | Shared debug log + workspace stage logging helpers |
| Copilot CLI backend | `common/runtime/copilot_cli.py` | Primary agentic CLI backend |
| Claude Code backend | `common/runtime/claude_code.py` | Optional compatible backend |
| Copilot Connect backend | `common/runtime/copilot_connect.py` | OpenAI-compatible backend / fallback |
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

- LLM-enabled agents (`team-lead`, `web`, `jira`, `scm`, `ui-design`) should load shared defaults from `common/.env` first, then apply their local `.env` overrides.
- Protected GitHub/SCM credential variables (`GH_TOKEN`, `GITHUB_TOKEN`, `COPILOT_GITHUB_TOKEN`, `SCM_TOKEN`, `SCM_USERNAME`, `SCM_PASSWORD`, `TEST_GITHUB_TOKEN`) are file-backed by default. Ambient host values must be ignored unless a launcher or test has already loaded its own `.env` and explicitly marks the child process with `CONSTELLATION_TRUSTED_ENV=1`.
- Runtime Git commands must use the isolated helper environment from `common.env_utils.build_isolated_git_env()` so agent subprocesses never read host Git credential helpers, host keychains, or user-level `~/.gitconfig`.
- `copilot-cli` runtime authentication is isolated as well: only `COPILOT_GITHUB_TOKEN` is supported for agent execution. Do not rely on `GH_TOKEN`, `GITHUB_TOKEN`, `gh auth`, or system keychain fallbacks inside agents.
- Launchers and integration tests must sanitize inherited host GitHub credentials before spawning subprocesses. Test scripts may use only file-backed values from `tests/.env` for GitHub auth.
- `compass` and `registry` remain non-agentic control-plane services; do not add runtime-adapter reasoning loops there unless the architecture changes.
- Task workspaces should keep `command-log.txt` and `stage-summary.json` under each agent subdirectory for auditability; runtime details belong inside `stage-summary.json` as `runtimeConfig`, not in a separate `runtime-config.json` file.
- In execution task workspaces, generated source files should live in the real cloned repository directory; `web-agent/` and similar agent subdirectories are for metadata and audit artifacts only.
- Web Agent branches should use deterministic naming based on Jira key plus orchestrator task id when available; only docs/tests-only changes may use `chore/...` naming without a ticket key.
- Boundary agents (Jira, SCM, UI Design, future Jenkins/Stitch-style integrations) must be discovered through Registry capabilities at runtime; do not hardcode their service URLs inside Team Lead or execution agents.
- Team Lead intake/gathering should use the agentic runtime to emit structured pending actions, but the code must still execute boundary calls itself through Registry-discovered capabilities. Do not let runtime output bypass A2A boundaries or directly hardcode external system access.
- Always construct `RegistryClient(REGISTRY_URL)` explicitly and pass it to `AgentDirectory(owner_id, registry_client)`. Never rely on the module-level `REGISTRY_URL` default inside `RegistryClient` — `load_dotenv` may not have run yet at import time.
- Registry now exposes topology metadata (`/topology`, `/events?sinceVersion=`); agents that call other agents should cache capability lookups and refresh on cache miss or topology change.
- Compass applies a final completeness gate to Team Lead results using shared-workspace evidence (review result, PR evidence, Jira workflow evidence) and may trigger a same-workspace follow-up cycle before marking the user task complete. The only exception is an explicit Team Lead validation checkpoint artifact (`metadata.validationCheckpoint=true`), which intentionally stops before dev dispatch and skips the completeness gate.
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
