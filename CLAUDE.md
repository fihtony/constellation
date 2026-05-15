# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with this repository.

## Commands

```bash
# Run all unit tests
pytest tests/unit/

# Run a single test file
pytest tests/unit/framework/test_workflow.py

# Run a single test by name
pytest tests/unit/framework/test_workflow.py::TestWorkflowBasic::test_linear_workflow

# Run integration tests (requires live services)
pytest tests/integration/ -m live

# Run e2e tests
pytest tests/e2e/

# Install dev dependencies
pip install -e ".[dev]"

# Start all services (v2 framework)
docker compose -f docker-compose-v2.yml up --build -d

# Check agent health
curl http://localhost:8000/health   # Compass
curl http://localhost:8030/health   # Team Lead
```

## Architecture

Constellation is a multi-agent system built on the A2A (Agent-to-Agent) protocol. All agent code lives in `agents/`, built on the shared `framework/` library.

### Two-layer design

Every agent uses **Graph outside, ReAct inside**:
- The macro lifecycle is a declarative `Workflow` graph (nodes + edges in `agents/<name>/agent.py`)
- Individual nodes call `runtime.run_agentic()` for open-ended LLM reasoning within a bounded step
- Exception: Compass uses ReAct-first (no graph) because it is free-form user interaction

### Framework modules (`framework/`)

| Module | Purpose |
|--------|---------|
| `agent.py` | `AgentDefinition`, `BaseAgent`, `AgentMode`, `ExecutionMode` |
| `workflow.py` | Declarative graph engine — `Workflow(edges=[...])`, `START`/`END` sentinels, conditional routing via `route` key |
| `runtime/adapter.py` | `AgentRuntimeAdapter` ABC + `get_runtime()` factory; backends: `connect-agent`, `claude_code`, `copilot_cli`, `codex_cli` |
| `a2a/` | A2A protocol types (`Task`, `Message`, `Artifact`, `TaskState`), HTTP server mixin, A2A client |
| `skills.py` | `SkillsRegistry` — hot-loads `skill.yaml` + `instructions.md` from `skills/` directory |
| `permissions.py` | `PermissionEngine` / `PermissionSet` — fail-closed tool and capability access control |
| `plugin.py` | `BasePlugin` / `PluginManager` — before/after hooks for agent, tool, LLM, and node lifecycle events |
| `config.py` | 4-layer config loader: global YAML → agent YAML → env vars → runtime overrides |
| `checkpoint.py` | `CheckpointService` ABC; `InMemoryCheckpointer` for tests, `SQLiteCheckpointer` for prod |
| `task_store.py` | In-process task persistence backing `GET /tasks/{id}` |
| `tools/` | `BaseTool` + `ToolResult`; `ToolRegistry` for tool registration |

### Agents (`agents/`)

| Agent | Port | Role |
|-------|------|------|
| `compass` | 8000 | Control plane: user-facing ReAct routing, task creation |
| `team_lead` | 8030 | Graph-driven: analyze → plan → dispatch → review → report |
| `web_dev` | on-demand | Graph-driven: setup → implement → test → PR → Jira update |
| `code_review` | on-demand | Code review execution |
| `jira` | — | Jira integration boundary agent |
| `scm` | — | GitHub/Bitbucket integration boundary agent |
| `ui_design` | — | Figma + Stitch design context boundary agent |

Each agent directory contains: `agent.py` (definition + workflow), `nodes.py` (graph node functions), `tools.py` (tool registrations), `config.yaml` (agent-specific config), `instructions/` or `prompts/` (system prompts), `Dockerfile`.

### Configuration

- Global config: `config/constellation.yaml`
- Per-agent config: `agents/<name>/config.yaml`
- Runtime secrets: environment variables (never in YAML)
- The `config.py` deep-merges these layers; lists replace rather than merge

### Skills (`skills/`)

Skills are hot-loaded domain knowledge injected into agent prompts. Each skill has `skill.yaml` (metadata + `allowed_tools`) and `instructions.md` (ReAct-format instructions). Current skills: `react-nextjs`, `testing`, `code-review`.

### Testing layout

```
tests/
  unit/
    framework/    # Pure unit tests — no network, use InMemoryCheckpointer
    agents/       # Per-agent unit tests with mocked services
  integration/    # Requires live external services (mark with @pytest.mark.live)
  e2e/            # Full chain tests
```

The `asyncio_mode = "auto"` pytest setting means all async test functions run automatically without `@pytest.mark.asyncio`.
