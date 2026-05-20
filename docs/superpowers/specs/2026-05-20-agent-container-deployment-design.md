# Agent Container Deployment Design

## Context

Constellation is a multi-agent system. We need to run agents in Docker/Rancher containers with:
- Persistent agents (compass, team-lead) always online
- Per-task agents (web-dev, code-review) launched on-demand
- Only compass and team-lead can launch other agents
- Workspace mapping: local `artifacts/` → container `/app/artifacts`
- Claude Code CLI credentials from host, not committed to git

## Goals

1. **Uniform Agentic CLI**: All agents use the same underlying CLI (Claude Code CLI, Copilot CLI, Connect Agent CLI). The CLI backend is swappable via an adapter interface.
2. **Generic Agent Launch**: Each agent declares how it should be launched in its registry entry. The launcher is runtime-agnostic (Docker or Rancher Desktop).
3. **A2A Communication**: All agent-to-agent communication uses the A2A protocol.

---

## Architecture

### 1. Agentic CLI Adapter

```python
# framework/runtime/adapter.py
class AgentRuntimeAdapter(ABC):
    """ABC for connecting to different agentic CLI backends."""

    @abstractmethod
    async def run_agentic(
        self, command: str, context: dict
    ) -> str:
        """Run an agentic command, return text output."""

    @abstractmethod
    async def launch_agent(
        self, agent_def: AgentDefinition, task_id: str, context: dict
    ) -> AgentInstance:
        """Launch an agent container/instance."""

    @abstractmethod
    def supports_runtime(self, runtime: str) -> bool:
        """Return True if this adapter supports the given runtime (docker|rancher)."""
```

Backends:
- `ClaudeCodeAdapter`: Connects to local `claude` CLI
- `CopilotCLIAdapter`: Connects to GitHub Copilot CLI
- `ConnectAgentAdapter`: HTTP-based connect-agent backend

Adapter selection via `AGENT_RUNTIME` env var (e.g., `claude-code`, `copilot-cli`, `connect-agent`).

### 2. Agent Launch Specification

Each agent declares its launch requirements in `AgentDefinition`:

```python
@dataclass
class AgentDefinition:
    # ... existing fields ...
    launch_spec: LaunchSpec = None

@dataclass
class LaunchSpec:
    cli: str = "claude-code"           # Which CLI backend to use
    image: str = ""                      # Docker image for containerized execution
    mount_docker_socket: bool = False    # Mount host Docker socket for nested Docker
    mount_artifact_root: bool = True     # Mount local artifacts/ to /app/artifacts
    env: dict = field(default_factory=dict)           # Static env vars
    pass_through_env: list[str] = field(default_factory=list)  # Env vars from host
    port: int = 0                          # Container port (0 = auto)
    memory: str = ""                      # Memory limit (e.g., "2g")
    startup_delay_seconds: float = 1.0   # Delay before health check
```

Registry stores the agent definition with launch spec. When team-lead dispatches a web-dev agent, it calls the adapter's `launch_agent()` which:
1. Reads the agent's `launch_spec`
2. Uses the appropriate runtime (Docker or Rancher)
3. Starts the container with bound artifacts path

### 3. Dual Runtime Launcher

```python
# framework/runtime/launcher.py
class AgentLauncher:
    def __init__(self, runtime: str = "docker"):
        self.runtime = resolve_container_runtime(runtime)

    def launch_agent(
        self, agent_def: AgentDefinition, task_id: str, context: dict
    ) -> AgentInstance:
        """Launch agent using Docker or Rancher based on CONTAINER_RUNTIME."""
        if self.runtime == "rancher":
            return self._launch_rancher(agent_def, task_id, context)
        return self._launch_docker(agent_def, task_id, context)

    def supports_runtime(self, runtime: str) -> bool:
        return runtime in ("docker", "rancher")
```

Runtime detection (from v1):
- `CONTAINER_RUNTIME=docker` → use standard `/var/run/docker.sock`
- `CONTAINER_RUNTIME=rancher` → use `~/.rd/docker.sock`

### 4. Permission Control for Agent Launching

New permission field in `PermissionSet`:

```python
@dataclass
class PermissionSet:
    agent_launching: bool = False   # Can launch other agents
    allowed_agents: list[str] = field(default_factory=list)  # Which agents can be launched
```

Compass and team-lead get `agent_launching=true`. Web-dev and code-review get `agent_launching=false`.

Enforcement in dispatch tools:
```python
class DispatchTool(BaseTool):
    def execute_sync(self, target_agent: str, **kwargs):
        perm = get_current_permission_engine()
        if not perm.permissions.agent_launching:
            raise PermissionDeniedError("Agent launching not permitted")
        if target_agent not in perm.permissions.allowed_agents:
            raise PermissionDeniedError(f"Agent {target_agent} not in allowed list")
        # proceed
```

### 5. Workspace Mapping

Local structure:
```
constellation/
  artifacts/
    <compass_id>/
      <task_id>/
        team-lead/
        web-dev/
        scm/
        ...
```

Container mounts:
```
./artifacts:/app/artifacts
```

Inside container, `ARTIFACT_ROOT=/app/artifacts`. Task workspace becomes `/app/artifacts/<compass_id>/<task_id>`.

The launcher resolves host paths for bind mounts using `_discover_host_source()` (from v1 `RancherLauncher`).

### 6. Claude Code CLI Credentials

Credentials stored in `~/.claude/settings.json`. When container starts:

1. Host mount: `~/.claude/:/home/appuser/.claude:ro` (read-only)
2. Or copy relevant settings to `.env` before container start

Env vars needed by Claude Code adapter:
- `ANTHROPIC_AUTH_TOKEN`
- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_MODEL` / `ANTHROPIC_DEFAULT_*_MODEL`
- `API_TIMEOUT_MS`
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`

Files:
- `.env.example`: All keys with placeholder values (committed)
- `.env`: Actual values from host (gitignored)

### 7. Docker Compose Files

**`docker-compose-v2.yml`** (Docker Desktop):
```yaml
services:
  compass:
    image: constellation-v2-compass:latest
    volumes:
      - ./artifacts:/app/artifacts
      - ~/.claude:/home/appuser/.claude:ro
    environment:
      CONTAINER_RUNTIME: docker
      ARTIFACT_ROOT: /app/artifacts

  team-lead:
    image: constellation-v2-team-lead:latest
    volumes:
      - ./artifacts:/app/artifacts
      - ~/.claude:/home/appuser/.claude:ro
    environment:
      CONTAINER_RUNTIME: docker
      ARTIFACT_ROOT: /app/artifacts
```

**`docker-compose-v2.rancher.yml`** (Rancher Desktop):
```yaml
services:
  compass:
    image: constellation-v2-compass:latest
    volumes:
      - ./artifacts:/app/artifacts
      - ~/.rd/docker.sock:/var/run/docker.sock
    environment:
      CONTAINER_RUNTIME: rancher
      ARTIFACT_ROOT: /app/artifacts
      DOCKER_SOCKET: /var/run/docker.sock
```

Launch per-task agents:
```bash
docker compose -f docker-compose-v2.yml run --rm web-dev ...
docker compose -f docker-compose-v2.rancher.yml run --rm web-dev ...
```

### 8. A2A Communication

All agents communicate via A2A protocol (existing):

```
Compass → TeamLead: dispatch_development_task (A2A Message)
TeamLead → WebDev: dispatch message with context (A2A Message)
WebDev → TeamLead: completion callback (A2A Message)
TeamLead → Compass: status update (A2A Message)
```

Persistent agents register with the A2A server. Per-task agents are launched, send messages, then terminate.

---

## Migration Notes

- Existing v1 `RancherLauncher` logic is reused via the `AgentLauncher` abstraction
- Existing `ConnectAgentAdapter` becomes `ConnectAgentRuntimeAdapter`
- Existing `AgentDefinition` fields remain, `launch_spec` added with sensible defaults
- `.env.example` template created, existing `.env` (if any) gitignored

---

## Delivery Checkpoints

1. `LaunchSpec` dataclass added to `framework/agent.py`
2. `AgentRuntimeAdapter` ABC defined in `framework/runtime/adapter.py`
3. `AgentLauncher` with Docker/Rancher support in `framework/runtime/launcher.py`
4. Permission field `agent_launching` added to `PermissionSet`
5. Dispatch tools check `agent_launching` permission before launching
6. `.env.example` created with Claude Code CLI variables
7. Dockerfiles mount artifacts and optionally `.claude` dir
8. `docker-compose-v2.rancher.yml` created
9. Per-task agents (web-dev, code-review) launch via `AgentLauncher`
10. E2E test passes for CSTL-1 task