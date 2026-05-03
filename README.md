# Constellation

Constellation is a multi-agent engineering system built on the [A2A (Agent-to-Agent) protocol](https://google.github.io/A2A/). It combines a control-plane entry point, a planning and review layer, and specialized execution and integration agents to coordinate complex software work through a consistent task interface. Its key strengths include fine-grained access control, enterprise-grade governance, automatic capability discovery, and a layered orchestration architecture that cleanly separates routing, coordination, and execution. Constellation is also adaptable by design and built to scale, using persistent integration agents alongside on-demand execution agents to support a wide range of engineering workflows efficiently and reliably.



## Highlights

- Clear orchestration model: Compass handles user entry, routing, and UI, while Team Lead handles planning, coordination, and review.
- Mixed agent runtime: persistent boundary agents manage integrations, and on-demand execution agents are launched only when needed.
- Consistent task contract: agents register capabilities, accept HTTP tasks, report progress, and return artifacts in the same way.

## Differentiators

- Separation of concerns: orchestration, external system access, and task execution are split into distinct agents instead of one large service.
- A2A plus MCP: Constellation uses A2A agents for workflow boundaries and MCP tools for selected integrations, which keeps the system modular.
- Built for engineering workflows: shared workspaces, async callbacks, and per-task agent launch make it suitable for longer-running development tasks.

## Architecture

```
Browser / API client
    └─► Compass Agent (control plane, UI, :8080)
             ├─► Office Agent (:8060)         — local document tasks
             └─► Team Lead Agent (:8030)      — planning, coordination, review
                      ├─► Capability Registry (:9000)
                      ├─► Jira Agent (:8010)       — Jira integration
                      ├─► SCM Agent (:8020)        — GitHub / Bitbucket integration
                      ├─► UI Design Agent (:8040)  — Figma + Stitch design context
                      └─► Execution Agents         — Android / Web / future iOS
```

## Quick Start

```bash
# 1. Copy and fill in environment files for each agent
cp compass/.env.example   compass/.env
cp jira/.env.example      jira/.env
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
| Compass | `compass/` | 8080 | Control plane, Web UI, user-facing routing |
| Team Lead | `team-lead/` | 8030 | Planning, coordination, review, and agent dispatch |
| Registry | `registry/` | 9000 | Capability discovery and instance tracking |
| Jira Agent | `jira/` | 8010 | Jira integration |
| SCM | `scm/` | 8020 | Source control integration |
| UI Design | `ui-design/` | 8040 | Design context from Figma and Stitch |
| Office | `office/` | 8060 | Local office and document workflows |
| Android | `android/` | on-demand | Task execution agent |
| Web | `web/` | on-demand | Task execution agent |

## Configuration

Each agent reads from its own `.env` file. Copy the corresponding `.env.example` and fill in credentials. Key variables:

| Variable | Description |
|----------|-------------|
| `OPENAI_BASE_URL` | OpenAI-compatible LLM endpoint |
| `OPENAI_MODEL` | Model name |
| `JIRA_TOKEN` | Jira API token |
| `JIRA_EMAIL` | Jira account email (Basic auth) |
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


