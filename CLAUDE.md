# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Constellation** is a multi-agent software development system built on the [A2A (Agent-to-Agent) protocol](https://google.github.io/A2A/). Each agent is an independent HTTP service that communicates through a central Registry for service discovery.

## Build and Run

```bash
# 1. Copy and fill in environment files for each service
cp compass/.env.example compass/.env
cp jira/.env.example jira/.env
cp scm/.env.example scm/.env
cp team-lead/.env.example team-lead/.env
cp web/.env.example web/.env
cp tests/.env.example tests/.env

# 2. Build Docker images for dynamic (per-task) agents
./build-agents.sh

# 3. Start all persistent services
docker compose up --build -d

# Web UI available at http://localhost:8080
```

## Running Tests

```bash
# End-to-end workflow test
python3 tests/test_e2e.py

# Web agent unit tests (starts a local agent automatically)
python3 tests/test_web_agent.py

# Web agent tests against an already-running container
python3 tests/test_web_agent.py --agent-url http://localhost:8050
```

Test credentials and target resources (Jira tickets, GitHub repos) are configured in `tests/.env` and `tests/agent_test_targets.json`.

## Architecture

### Service Map

```
Browser / API Client
    └─► Compass (:8080)  ← control plane, Web UI, workflow orchestrator
             ├─► Registry (:9000)          — service discovery, capability lookup
             ├─► Jira Agent (:8010)         — Jira Cloud integration
             ├─► SCM Agent (:8020)          — GitHub / Bitbucket integration
             ├─► UI Design Agent (:8040)    — Figma integration
             └─► Team Lead Agent (dynamic)  — task analysis, coordination, code review
                  └─► Web Agent (dynamic)   — full-stack implementation
```

**Persistent services** (always running): Registry, Compass, Jira, SCM, UI Design.  
**Dynamic agents** (per-task, launched by Compass via Docker socket): Team Lead, Web.

### A2A Communication Pattern

All agents expose a standard interface:

```
POST /message:send   → starts or resumes a task, returns immediately with task state
GET  /.well-known/agent-card.json  → capability declaration
```

Async callbacks flow back to Compass at `POST /tasks/{task_id}/callbacks`. Compass also accepts progress reports at `POST /tasks/{task_id}/progress`.

### Typical Task Flow

1. User submits task to Compass → Compass creates task in `ROUTING` state
2. Compass queries Registry for `team-lead.task.analyze` capability
3. No idle instance found → `Launcher` starts a Team Lead container via Docker socket
4. Team Lead registers with Registry, Compass calls `POST /message:send`
5. Team Lead fetches Jira/design context, generates an execution plan
6. Team Lead launches a Web Agent container for implementation
7. Web Agent implements code, calls SCM Agent to create a PR, then POSTs callback to Compass
8. Team Lead performs code review; may iterate (up to `MAX_REVIEW_CYCLES`, default 2)
9. Team Lead POSTs final callback → Compass marks task `TASK_STATE_COMPLETED`

### INPUT_REQUIRED (human-in-the-loop)

When Team Lead needs user input it POSTs `TASK_STATE_INPUT_REQUIRED` to Compass. The UI presents the question. User replies with `POST /message:send` using `contextId=task_id`, which Compass routes back to the waiting Team Lead thread.

### Dynamic Agent Configuration

`registry-config.json` in each dynamic agent directory declares how Compass should launch and scale it:

```json
{
  "executionMode": "per-task",
  "launchSpec": { "image": "constellation-web-agent:latest", "port": 8050, ... },
  "scalingPolicy": { "maxInstances": 5, "perInstanceConcurrency": 1, "idleTimeoutSeconds": 120 }
}
```

### Key Shared Modules (`common/`)

| Module | Purpose |
|---|---|
| `launcher.py` | Docker container lifecycle (start, stop, log streaming) |
| `registry_client.py` | Registry REST client |
| `instance_reporter.py` | Heartbeat reporting to Registry (every 30 s) |
| `task_store.py` | In-memory task state |
| `artifact_store.py` | File-system artifact persistence |
| `llm_client.py` | OpenAI-compatible LLM client |
| `message_utils.py` | A2A message parsing and artifact construction |
| `policy.py` | Access policy evaluation |

## Key Configuration

### LLM (all agents)

| Variable | Default |
|---|---|
| `CONTAINER_RUNTIME` | `docker` |
| `OPENAI_BASE_URL` | runtime-aware default: `http://localhost:1288/v1` on host, `http://host.docker.internal:1288/v1` in Docker containers, `http://host.rancher-desktop.internal:1288/v1` in Rancher containers |
| `OPENAI_MODEL` | `gpt-5-mini` |
| `ALLOW_MOCK_FALLBACK` | — (set to `1` for offline/test mode) |

For launcher agents:
- Inside containers, always mount the host Docker-compatible socket at `/var/run/docker.sock` and set `DOCKER_SOCKET=/var/run/docker.sock` in the container env.
- On the host, Rancher Desktop uses `~/.rd/docker.sock` by default; set `DOCKER_SOCKET` only when a host process needs to talk to Rancher directly.
- Nested per-task launches must re-bind the host-side socket source discovered from the current container mount, not the current container path verbatim.

### Jira Agent (required)

```
JIRA_TOKEN, JIRA_EMAIL, JIRA_BASE_URL, JIRA_AUTH_MODE (basic|bearer)
```

### SCM Agent (required)

```
SCM_TOKEN, SCM_PROVIDER (github|bitbucket), SCM_USERNAME (Bitbucket only)
```

### Compass (optional overrides)

```
A2A_ACK_TIMEOUT_SECONDS   (default 15)
A2A_TASK_TIMEOUT_SECONDS  (default 3600)
ARTIFACT_ROOT             (default /app/artifacts)
DYNAMIC_AGENT_NETWORK     (Docker network for launched containers)
```

## Health Checks and Debugging

```bash
# Service health
curl http://localhost:9000/health   # Registry
curl http://localhost:8080/health   # Compass

# Registry state
curl http://localhost:9000/agents
curl "http://localhost:9000/query?capability=team-lead.task.analyze"

# Compass debug
curl http://localhost:8080/debug/agent-logs

# Service logs
docker compose logs -f compass
docker compose logs -f team-lead
```

## Important Notes

- Registry and TaskStore are **in-memory only**; state is lost on restart.
- Dynamic agents require the Docker socket to be mounted (`/var/run/docker.sock`).
- LLM prompts live in `web/prompts.py` and `team-lead/prompts.py` — edit these to tune agent behavior without changing orchestration logic.
- `build-agents.sh` must be re-run after any changes to `team-lead/` or `web/` before the updated code takes effect in running containers.
