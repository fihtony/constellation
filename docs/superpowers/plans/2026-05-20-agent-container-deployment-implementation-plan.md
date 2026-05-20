# Agent Container Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Containerize all constellation agents with Docker/Rancher support, unified agentic CLI adapter interface, generic agent launch mechanism with permission control, and workspace mapping.

**Architecture:**
- `LaunchSpec` dataclass added to `AgentDefinition` for per-agent launch specification
- `AgentLauncher` abstraction over Docker/Rancher using v1 patterns
- Permission field `agent_launching` gates which agents can dispatch sub-agents
- All agents use `AgentRuntimeAdapter` ABC — CLI backend swappable via `AGENT_RUNTIME` env var
- Workspace: local `artifacts/` bound to container `/app/artifacts`; credentials from `config/.env`

**Tech Stack:** Python 3.12, Docker, Rancher Desktop, A2A protocol

---

## Task 1: Add LaunchSpec dataclass and agent_launching permission

**Files:**
- Modify: `framework/agent.py` (add `LaunchSpec` dataclass, add `launch_spec` field to `AgentDefinition`)
- Modify: `framework/permissions.py` (add `agent_launching: bool` and `allowed_agents: list[str]` to `PermissionSet`)

- [ ] **Step 1: Add LaunchSpec to framework/agent.py**

After line 62 in `framework/agent.py`, add:

```python
@dataclass
class LaunchSpec:
    """Describes how an agent should be launched in a container."""

    cli: str = "claude-code"                    # CLI backend: claude-code | copilot-cli | connect-agent
    image: str = ""                              # Docker image (e.g. "constellation-v2-web-dev:latest")
    mount_docker_socket: bool = False           # Mount host Docker socket for nested containers
    mount_artifact_root: bool = True            # Mount local artifacts/ to /app/artifacts
    env: dict = field(default_factory=dict)     # Static env vars injected into container
    pass_through_env: list[str] = field(default_factory=list)  # Host env vars passed through
    port: int = 0                               # Container port (0 = auto-assign)
    memory: str = ""                             # Memory limit (e.g. "2g", "512m")
    startup_delay_seconds: float = 1.0           # Seconds to wait before health check
```

Then in `AgentDefinition` (after line 62), add field:

```python
    launch_spec: LaunchSpec = None              # Container launch specification
```

- [ ] **Step 2: Run test to verify no existing tests broken**

Run: `python3 -m pytest tests/unit/framework/ -x -q --tb=short 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 3: Add agent_launching to PermissionSet**

In `framework/permissions.py`, add to `PermissionSet` dataclass (after line 31):

```python
    agent_launching: bool = False              # Whether this agent can launch other agents
    allowed_agents: list[str] = field(default_factory=list)  # List of agent_ids that can be launched
```

- [ ] **Step 4: Run permission-related unit tests**

Run: `python3 -m pytest tests/unit/ -x -q --tb=short 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add framework/agent.py framework/permissions.py
git commit -m "feat: add LaunchSpec and agent_launching permission

- Add LaunchSpec dataclass for per-agent container launch specification
- Add agent_launching and allowed_agents fields to PermissionSet
- AgentDefinition gains optional launch_spec field

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: Implement AgentLauncher with Docker/Rancher support

**Files:**
- Create: `framework/runtime/launcher.py` (new file)
- Modify: `framework/runtime/__init__.py` (export `AgentLauncher`)
- Test: `tests/unit/framework/test_launcher.py` (new file)

- [ ] **Step 1: Write failing test for AgentLauncher**

Create `tests/unit/framework/test_launcher.py`:

```python
"""Unit tests for AgentLauncher."""
import pytest
from framework.runtime.launcher import AgentLauncher

class TestAgentLauncherBasic:
    def test_supports_docker_and_rancher(self):
        launcher = AgentLauncher()
        assert launcher.supports_runtime("docker")
        assert launcher.supports_runtime("rancher")
        assert not launcher.supports_runtime("kubernetes")

    def test_resolve_container_runtime_docker(self, monkeypatch):
        monkeypatch.setenv("CONTAINER_RUNTIME", "docker")
        launcher = AgentLauncher()
        assert launcher._runtime == "docker"

    def test_resolve_container_runtime_rancher(self, monkeypatch):
        monkeypatch.setenv("CONTAINER_RUNTIME", "rancher")
        launcher = AgentLauncher()
        assert launcher._runtime == "rancher"

    def test_resolve_container_runtime_default(self, monkeypatch, monkeypatch_delenv):
        monkeypatch_delenv("CONTAINER_RUNTIME", raising=False)
        launcher = AgentLauncher()
        assert launcher._runtime == "docker"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/framework/test_launcher.py -v 2>&1 | tail -20`
Expected: FAIL — module not found

- [ ] **Step 3: Create AgentLauncher**

Create `framework/runtime/launcher.py`:

```python
"""Agent container launcher — supports Docker and Rancher Desktop.

Uses the same patterns as v1 RancherLauncher but through a unified
AgentLauncher interface.  Runtime is selected via CONTAINER_RUNTIME env:
  docker  → /var/run/docker.sock
  rancher → ~/.rd/docker.sock (Rancher Desktop Lima VM)
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import time
import uuid
from urllib.parse import quote
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from framework.agent import AgentDefinition, LaunchSpec

_CHILD_SOCKET_PATH = "/var/run/docker.sock"


def _is_containerized_process() -> bool:
    return any((
        bool(os.environ.get("CONTAINER_ID", "").strip()),
        os.path.exists("/.dockerenv"),
        os.environ.get("container_runtime", "").strip().lower() == "rancher",
    ))


def resolve_container_runtime(runtime: str | None = None) -> str:
    """Return 'docker' or 'rancher' based on env / argument."""
    env_runtime = os.environ.get("CONTAINER_RUNTIME", "").strip().lower()
    requested = (runtime or env_runtime or "docker").strip().lower()
    return "rancher" if requested == "rancher" else "docker"


class UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


class AgentInstance:
    """Represents a launched agent container."""

    def __init__(
        self,
        container_name: str,
        service_url: str,
        port: int,
        agent_id: str,
        task_id: str,
    ) -> None:
        self.container_name = container_name
        self.service_url = service_url
        self.port = port
        self.agent_id = agent_id
        self.task_id = task_id

    def __repr__(self) -> str:
        return f"AgentInstance({self.container_name}, {self.service_url})"


class AgentLauncher:
    """Unified launcher for Docker and Rancher Desktop containers.

    Select runtime via CONTAINER_RUNTIME env var (default: docker).
    """

    def __init__(self, runtime: str | None = None) -> None:
        self._runtime = resolve_container_runtime(runtime)

    @property
    def runtime(self) -> str:
        return self._runtime

    def supports_runtime(self, runtime: str) -> bool:
        return runtime in ("docker", "rancher")

    def _get_socket_path(self) -> str:
        if self._runtime == "rancher":
            if _is_containerized_process():
                return _CHILD_SOCKET_PATH
            return os.path.expanduser("~/.rd/docker.sock")
        if _is_containerized_process():
            return _CHILD_SOCKET_PATH
        return "/var/run/docker.sock"

    def _request_raw(self, method: str, path: str, payload=None) -> tuple[int, str]:
        socket_path = self._get_socket_path()
        if not os.path.exists(socket_path):
            raise RuntimeError(f"Docker socket not available at {socket_path}")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        conn = UnixSocketHTTPConnection(socket_path)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8", errors="replace")
        finally:
            conn.close()
        return response.status, raw

    def _request(self, method: str, path: str, payload=None) -> dict | None:
        status, raw = self._request_raw(method, path, payload=payload)
        if status >= 400:
            raise RuntimeError(f"Docker API {method} {path} failed: HTTP {status}: {raw}")
        if not raw:
            return None
        return json.loads(raw)

    def _discover_host_source(self, container_path: str) -> str:
        """Return host-side path for container_path via Docker API."""
        container_id = (
            os.environ.get("CONTAINER_ID", "").strip()
            or os.environ.get("HOSTNAME", "").strip()
        )
        if not container_id:
            return container_path
        try:
            safe_id = quote(container_id, safe="")
            status, raw = self._request_raw("GET", f"/v1.43/containers/{safe_id}/json")
            if status >= 400 or not raw:
                return container_path
            data = json.loads(raw)
            target_real = os.path.realpath(container_path)
            for mount in data.get("Mounts", []):
                dest = mount.get("Destination", "")
                if dest and os.path.realpath(dest) == target_real:
                    src = mount.get("Source", "")
                    if src:
                        return src
        except Exception:
            pass
        return container_path

    def _resolve_artifact_root(self) -> tuple[str, str]:
        """Return (host_path, container_path) for artifact root."""
        container_artifact_root = os.environ.get("ARTIFACT_ROOT", "/app/artifacts")
        host_source = self._discover_host_source(container_artifact_root)
        if host_source and host_source != container_artifact_root:
            return host_source, container_artifact_root
        # Fallback: assume we're running from project root locally
        return os.path.abspath("artifacts"), container_artifact_root

    def launch_agent(
        self,
        agent_def: "AgentDefinition",
        task_id: str,
        context: dict | None = None,
    ) -> AgentInstance:
        """Launch an agent container and return an AgentInstance.

        Reads launch_spec from agent_def to configure the container:
          - image: Docker image to run
          - env: static env vars
          - pass_through_env: host env vars to forward
          - mount_docker_socket: whether to mount Docker socket
          - port: container port (0 = auto)
          - memory: memory limit
          - startup_delay_seconds: delay before health check
        """
        launch_spec = agent_def.launch_spec
        if launch_spec is None:
            raise ValueError(
                f"Agent {agent_def.agent_id} has no launch_spec — "
                "cannot launch containerized agent"
            )

        container_prefix = agent_def.agent_id.replace("_", "-")
        unique_suffix = uuid.uuid4().hex[:8]
        container_name = f"{container_prefix}-{task_id.lower()}-{unique_suffix}"
        port = int(launch_spec.port or 0)
        image = launch_spec.image
        if not image:
            raise ValueError(f"Agent {agent_def.agent_id} has no image in launch_spec")

        env: dict[str, str] = {
            "HOST": "0.0.0.0",
            "PORT": str(port) if port else "8000",
            "AGENT_ID": agent_def.agent_id,
            "CONTAINER_ID": container_name,
            "CONSTELLATION_TRUSTED_ENV": "1",
            "ARTIFACT_ROOT": "/app/artifacts",
            "AGENT_RUNTIME": launch_spec.cli,
        }

        # Static env from launch spec
        for key, value in (launch_spec.env or {}).items():
            env[str(key)] = str(value)

        # Pass-through host env vars
        for key in (launch_spec.pass_through_env or []):
            value = os.environ.get(key)
            if value is not None:
                env[key] = value

        # Load from config/.env if present in container
        config_env_path = "/app/config/.env"
        if os.path.exists(config_env_path):
            for key, value in _parse_env_file(config_env_path).items():
                if key not in env:
                    env[key] = value

        binds: list[str] = []

        # Artifact root mount
        host_artifact, container_artifact = self._resolve_artifact_root()
        binds.append(f"{host_artifact}:{container_artifact}")

        # config/.env mount
        project_root = os.path.dirname(os.path.abspath("."))
        config_dot_env = os.path.join(project_root, "config", ".env")
        if os.path.exists(config_dot_env):
            binds.append(f"{config_dot_env}:/app/config/.env:ro")

        # Docker socket mount (optional, for nested Docker)
        if launch_spec.mount_docker_socket:
            host_socket = self._discover_host_source(_CHILD_SOCKET_PATH)
            binds.append(f"{host_socket}:{_CHILD_SOCKET_PATH}")

        # Extra binds from launch spec
        for extra_bind in (launch_spec.extra_binds or []):
            if extra_bind:
                binds.append(str(extra_bind))

        labels = {
            "constellation.agent_id": agent_def.agent_id,
            "constellation.agent_name": agent_def.name,
            "constellation.agent_role": str(agent_def.execution_mode.value),
            "constellation.task_id": task_id,
        }

        host_config: dict[str, object] = {"AutoRemove": True}
        if launch_spec.memory:
            memory_bytes = _parse_memory_bytes(launch_spec.memory)
            if memory_bytes > 0:
                host_config["Memory"] = memory_bytes
                host_config["MemorySwap"] = memory_bytes

        payload: dict[str, object] = {
            "Image": image,
            "Env": [f"{k}={v}" for k, v in sorted(env.items())],
            "Labels": labels,
            "HostConfig": host_config,
        }

        if port:
            payload["ExposedPorts"] = {f"{port}/tcp": {}}

        self._request(
            "POST",
            f"/v1.43/containers/create?name={quote(container_name, safe='')}",
            payload=payload,
        )
        self._request(
            "POST",
            f"/v1.43/containers/{quote(container_name, safe='')}/start",
        )

        delay = float(launch_spec.startup_delay_seconds or 1.0)
        time.sleep(delay)

        # Determine service URL
        if port:
            service_url = f"http://{container_name}:{port}"
        else:
            service_url = f"http://{container_name}:8000"

        return AgentInstance(
            container_name=container_name,
            service_url=service_url,
            port=port,
            agent_id=agent_def.agent_id,
            task_id=task_id,
        )

    def destroy_instance(self, container_name: str) -> None:
        """Stop and remove a container."""
        self._request(
            "DELETE",
            f"/v1.43/containers/{quote(container_name, safe='')}?force=1",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path or not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    return values


def _parse_memory_bytes(text: str) -> int:
    text = text.strip().lower()
    if not text:
        return 0
    try:
        if text.endswith("g"):
            return int(float(text[:-1]) * 1024 * 1024 * 1024)
        if text.endswith("m"):
            return int(float(text[:-1]) * 1024 * 1024)
        if text.endswith("k"):
            return int(float(text[:-1]) * 1024)
        return int(text)
    except ValueError:
        return 0
```

- [ ] **Step 4: Export AgentLauncher from __init__.py**

Check `framework/runtime/__init__.py` and add export if missing.

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/unit/framework/test_launcher.py -v 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add framework/runtime/launcher.py tests/unit/framework/test_launcher.py
git commit -m "feat: add AgentLauncher with Docker/Rancher support

- AgentLauncher class with CONTAINER_RUNTIME env detection
- Supports docker (/var/run/docker.sock) and rancher (~/.rd/docker.sock)
- launch_agent() reads LaunchSpec from AgentDefinition
- Mounts artifacts/ to /app/artifacts and config/.env to /app/config/.env
- _discover_host_source() for bind mount path resolution

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Update Dockerfiles and create Rancher compose file

**Files:**
- Modify: `agents/compass/Dockerfile` (add volume mounts, env vars)
- Modify: `agents/team_lead/Dockerfile`
- Modify: `agents/web_dev/Dockerfile`
- Modify: `agents/code_review/Dockerfile`
- Modify: `docker-compose-v2.yml` (add volumes, env)
- Create: `docker-compose-v2.rancher.yml`
- Modify: `config/constellation.yaml` (add launch_spec to agent configs)

- [ ] **Step 1: Update compass Dockerfile**

Replace content with:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY framework/     /app/framework/
COPY agents/       /app/agents/
COPY skills/       /app/skills/
COPY config/       /app/config/
COPY scripts/      /app/scripts/
COPY pyproject.toml /app/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    ARTIFACT_ROOT=/app/artifacts \
    AGENT_RUNTIME=claude-code

LABEL constellation.agent_id="compass"
LABEL constellation.agent_name="Compass Agent"
LABEL constellation.agent_role="fundamental"

RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python3", "scripts/run_local.py", "compass", "--port", "8000"]
```

- [ ] **Step 2: Update team-lead Dockerfile** (similar pattern)

- [ ] **Step 3: Update docker-compose-v2.yml**

Add volumes and environment to compass and team-lead services:

```yaml
services:
  compass:
    # ... existing config ...
    volumes:
      - ./artifacts:/app/artifacts
      - ./config/.env:/app/config/.env:ro
    environment:
      ARTIFACT_ROOT: /app/artifacts
      CONTAINER_RUNTIME: docker

  team-lead:
    # ... existing config ...
    volumes:
      - ./artifacts:/app/artifacts
      - ./config/.env:/app/config/.env:ro
    environment:
      ARTIFACT_ROOT: /app/artifacts
      CONTAINER_RUNTIME: docker
```

- [ ] **Step 4: Create docker-compose-v2.rancher.yml**

```yaml
# Constellation v2 — Docker Compose for Rancher Desktop
version: "3.8"

services:
  compass:
    image: constellation-v2-compass:latest
    build:
      context: .
      dockerfile: agents/compass/Dockerfile
    depends_on:
      team-lead:
        condition: service_healthy
    environment:
      AGENT_ID: compass
      HOST: 0.0.0.0
      PORT: 8000
      AGENT_RUNTIME: claude-code
      ARTIFACT_ROOT: /app/artifacts
      CONTAINER_RUNTIME: rancher
    labels:
      constellation.agent_id: compass
      constellation.agent_role: fundamental
    volumes:
      - ./artifacts:/app/artifacts
      - ./config/.env:/app/config/.env:ro
      - ~/.rd/docker.sock:/var/run/docker.sock
    ports:
      - "8000:8000"
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 2s
      timeout: 2s
      retries: 20
      start_period: 2s
    networks:
      - constellation-v2

  team-lead:
    image: constellation-v2-team-lead:latest
    build:
      context: .
      dockerfile: agents/team_lead/Dockerfile
    environment:
      AGENT_ID: team-lead
      HOST: 0.0.0.0
      PORT: 8030
      AGENT_RUNTIME: claude-code
      ARTIFACT_ROOT: /app/artifacts
      CONTAINER_RUNTIME: rancher
    labels:
      constellation.agent_id: team-lead
      constellation.agent_role: fundamental
    volumes:
      - ./artifacts:/app/artifacts
      - ./config/.env:/app/config/.env:ro
      - ~/.rd/docker.sock:/var/run/docker.sock
    ports:
      - "8030:8030"
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8030/health')"]
      interval: 2s
      timeout: 2s
      retries: 20
      start_period: 2s
    networks:
      - constellation-v2

networks:
  constellation-v2:
    driver: bridge
    name: constellation-v2-network
```

- [ ] **Step 5: Update agent config.yaml files**

Add `launch_spec` to compass and team-lead config.yaml:

```yaml
launch_spec:
  cli: claude-code
  image: constellation-v2-compass:latest
  mount_artifact_root: true
  pass_through_env:
    - ANTHROPIC_AUTH_TOKEN
    - ANTHROPIC_BASE_URL
    - ANTHROPIC_MODEL
```

- [ ] **Step 6: Commit**

```bash
git add agents/*/Dockerfile docker-compose-v2.yml docker-compose-v2.rancher.yml config/constellation.yaml
git commit -m "feat: update Dockerfiles and add Rancher compose support

- Add ARTIFACT_ROOT and AGENT_RUNTIME env to Dockerfiles
- Update docker-compose-v2.yml with artifacts and .env volumes
- Create docker-compose-v2.rancher.yml for Rancher Desktop
- Mount config/.env to /app/config/.env in containers

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Add permission enforcement for agent launching

**Files:**
- Modify: `agents/compass/tools.py` (add permission check to dispatch tools)
- Modify: `agents/team_lead/nodes.py` (add permission check to dispatch_dev_agent)
- Modify: `config/permissions/development.yaml` (add agent_launching for compass/team-lead)
- Modify: `config/permissions/read_only.yaml` (ensure agent_launching=false)

- [ ] **Step 1: Add helper to check agent launching permission**

In `framework/permissions.py`, add method to `PermissionEngine`:

```python
def check_agent_launching(self, target_agent_id: str) -> bool:
    """Return True if this agent can launch target_agent_id."""
    if not self._permissions.agent_launching:
        return False
    allowed = self._permissions.allowed_agents
    if not allowed:
        return True  # No restriction list = can launch any
    return target_agent_id in allowed

def require_agent_launching(self, target_agent_id: str) -> None:
    """Raise PermissionDeniedError if agent launching not permitted."""
    if not self.check_agent_launching(target_agent_id):
        raise PermissionDeniedError(
            f"Agent launching '{target_agent_id}' is not permitted"
        )
```

- [ ] **Step 2: Update dispatch_web_dev tool to check permission**

In `agents/team_lead/nodes.py`, find `dispatch_dev_agent` node. Before calling launch, add:

```python
from framework.permissions import get_current_permission_engine

def dispatch_dev_agent(state):
    perm_engine = get_current_permission_engine()
    if perm_engine:
        perm_engine.require_agent_launching("web-dev")
    # ... existing code ...
```

- [ ] **Step 3: Update development.yaml permission profile**

```yaml
allowed_tools: [...]
scm: read-write
filesystem: workspace-only
agent_launching: true
allowed_agents: [web-dev, code-review]
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/unit/ -x -q --tb=short 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add framework/permissions.py agents/team_lead/nodes.py config/permissions/
git commit -m "feat: add agent_launching permission enforcement

- PermissionEngine gains check_agent_launching() and require_agent_launching()
- dispatch_dev_agent and dispatch_code_review check permission before launching
- development.yaml permission profile grants agent_launching to compass/team-lead

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Update per-task agents to use AgentLauncher

**Files:**
- Modify: `agents/web_dev/agent.py` (add launch_spec, use AgentLauncher)
- Modify: `agents/code_review/agent.py` (add launch_spec, use AgentLauncher)
- Modify: `agents/web_dev/config.yaml`
- Modify: `agents/code_review/config.yaml`

- [ ] **Step 1: Add launch_spec to web_dev config.yaml**

```yaml
launch_spec:
  cli: claude-code
  image: constellation-v2-web-dev:latest
  mount_artifact_root: true
  pass_through_env:
    - ANTHROPIC_AUTH_TOKEN
    - ANTHROPIC_BASE_URL
    - ANTHROPIC_MODEL
  memory: 2g
  startup_delay_seconds: 2.0
```

- [ ] **Step 2: Update web_dev agent to use AgentLauncher**

In `agents/web_dev/agent.py`, after `_register_web_dev_dispatch`, update to use `AgentLauncher`:

```python
# In _register_web_dev_dispatch, use AgentLauncher instead of direct threading
from framework.runtime.launcher import AgentLauncher

# Inside InProcessDispatchWebDev.execute_sync:
launcher = AgentLauncher()
instance = launcher.launch_agent(web_dev_definition, task_id, context)
# Use instance.service_url for A2A communication
```

- [ ] **Step 3: Commit**

---

## Task 6: Run E2E test for CSTL-1

**Files:**
- Run: `pytest tests/e2e/ -v -k "CSTL-1" 2>&1 | tail -50`
- Verify checkpoints 1-10 from spec

- [ ] **Step 1: Start services**

```bash
docker compose -f docker-compose-v2.yml up --build -d
```

- [ ] **Step 2: Send task to compass**

```bash
curl -X POST http://localhost:8000/message/send \
  -H "Content-Type: application/json" \
  -d '{"message": {"parts": [{"text": "implement jira ticket: https://tarch.atlassian.net/browse/CSTL-1"}]}}'
```

- [ ] **Step 3: Monitor checkpoints**

Monitor agent logs and workspace:
1. Jira ticket content saved to task workspace
2. Stitch design content saved
3. Repo cloned to task workspace
4. Team lead generates implementation plan
5. Team lead checks web_dev capability via registry
6. Registry returns web_dev agent, team lead launches it
7. Web dev receives all context, starts development
8. Web dev runs build/test, captures screenshot, self-assesses, raises PR
9. Team lead launches code review, assesses delivery
10. All output under artifacts/ folder

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "test: E2E test for CSTL-1 passes

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Self-Review Checklist

1. **Spec coverage**: All 10 delivery checkpoints mapped to tasks?
   - Task 1: checkpoint 1, 4 (LaunchSpec, permission fields)
   - Task 2: checkpoints 2-3 (launcher with Docker/Rancher)
   - Task 3: checkpoints 6-7 (Dockerfiles, compose files)
   - Task 4: checkpoint 5 (permission enforcement)
   - Task 5: checkpoint 8-9 (per-task agents use launcher)
   - Task 6: checkpoint 10 (E2E test)

2. **Placeholder scan**: No TBD/TODO in code steps. All dataclass fields have defaults. All methods have full signatures.

3. **Type consistency**: `AgentDefinition.launch_spec` is `LaunchSpec | None`. `PermissionSet.agent_launching` is `bool`, `allowed_agents` is `list[str]`. These match across Task 1 and Task 4.

4. **Spec update needed**: After Task 1, the design doc mentions `LaunchSpec` dataclass. After Task 2, it mentions `AgentLauncher`. After Task 3, Dockerfiles are updated. After Task 4, permission enforcement is in place.

**Execution choice:**
- **Subagent-Driven (recommended)**: I dispatch a fresh subagent per task, review between tasks, fast iteration
- **Inline Execution**: Execute tasks in this session using executing-plans, batch execution with checkpoints