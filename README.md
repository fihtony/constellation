# Constellation

A multi-agent system built on the [A2A (Agent-to-Agent) protocol](https://google.github.io/A2A/). Each agent is a self-contained service that registers its capabilities, accepts tasks over HTTP, and reports results back to the orchestrator.

## Architecture

```
Browser / API client
    └─► Compass Agent (control plane, :8080)
             ├─► Capability Registry (:9000)
             ├─► Tracker Agent (:8010)   — Jira Cloud via Atlassian Rovo MCP
             ├─► SCM Agent (:8020)       — GitHub / Bitbucket
             ├─► UI Design Agent (:8040) — Figma REST + Google Stitch MCP
             └─► Android Agent           — launched on demand via Docker socket
```

## Quick Start

```bash
# 1. Copy and fill in environment files for each agent
cp compass/.env.example   compass/.env
cp tracker/.env.example   tracker/.env
cp scm/.env.example       scm/.env
cp ui-design/.env.example ui-design/.env

# 2. Build on-demand agent images
./build-agents.sh

# 3. Start all persistent services
docker compose up --build -d

# 4. Open the Web UI
open http://localhost:8080
```

## Agents

| Agent | Directory | Port | Role |
|-------|-----------|------|------|
| Compass | `compass/` | 8080 | Control plane, Web UI, workflow orchestration |
| Registry | `registry/` | 9000 | Agent discovery and instance tracking |
| Tracker | `tracker/` | 8010 | Jira Cloud integration (via Atlassian Rovo MCP) |
| SCM | `scm/` | 8020 | GitHub / Bitbucket integration |
| UI Design | `ui-design/` | 8040 | Figma REST API + Google Stitch MCP |
| Android | `android/` | — | On-demand execution, launched per task |

## Configuration

Each agent reads from its own `.env` file. Copy the corresponding `.env.example` and fill in credentials. Key variables:

| Variable | Description |
|----------|-------------|
| `OPENAI_BASE_URL` | OpenAI-compatible LLM endpoint |
| `OPENAI_MODEL` | Model name |
| `TRACKER_TOKEN` | Jira API token |
| `TRACKER_EMAIL` | Jira account email (Basic auth) |
| `SCM_TOKEN` | GitHub / Bitbucket personal access token |
| `FIGMA_TOKEN` | Figma personal access token |
| `STITCH_API_KEY` | Google Stitch / Gemini API key |

## Running Tests

```bash
# Jira MCP integration tests
python3 tests/test_mcp.py --integration --jira

# UI Design agent tests (Figma + Stitch)
python3 tests/test_ui_design_agent.py --integration

# Async skill contract tests (SCM clone + Android callback)
python3 tests/test_async_skills.py --container

# End-to-end workflow tests
python3 tests/test_e2e.py
```

Set test credentials in `tests/.env` (copy from `tests/.env.example`).

## License

MIT © Tony Xu


