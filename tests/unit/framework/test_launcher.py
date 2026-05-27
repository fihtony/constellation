"""Unit tests for the per-task container launcher."""

from pathlib import Path

from framework.agent import AgentDefinition, LaunchSpec
from framework.launcher import Launcher


def test_launch_instance_mounts_only_task_workspace(monkeypatch):
    """Launcher should expose only the current task workspace to per-task agents."""

    monkeypatch.setenv("ARTIFACT_ROOT", "/app/artifacts")
    monkeypatch.setenv("REGISTRY_URL", "http://registry:9000")

    launcher = Launcher(socket_path="/tmp/fake-docker.sock")
    requests = []

    def fake_request(method, path, payload=None):
        requests.append((method, path, payload))
        return {}

    monkeypatch.setattr(launcher, "_request", fake_request)
    monkeypatch.setattr(launcher, "resolve_host_path", lambda path: "/host/artifacts" if path == "/app/artifacts" else path)
    monkeypatch.setattr("framework.launcher.time.sleep", lambda _: None)

    agent = AgentDefinition(
        agent_id="office",
        name="Office Agent",
        description="Office",
        launch_spec=LaunchSpec(image="constellation-v2-office:latest", port=8060),
    )

    launcher.launch_instance(agent, "task-123")

    create_requests = [payload for method, path, payload in requests if method == "POST" and path.startswith("/v1.43/containers/create")]
    assert len(create_requests) == 1

    create_payload = create_requests[0]
    assert "/host/artifacts/task-123:/app/artifacts/task-123" in create_payload["HostConfig"]["Binds"]
    assert "/host/artifacts:/app/artifacts" not in create_payload["HostConfig"]["Binds"]
    assert "ARTIFACT_ROOT=/app/artifacts" in create_payload["Env"]
    assert "CONSTELLATION_TASK_WORKSPACE=/app/artifacts/task-123" in create_payload["Env"]


def test_launch_instance_passes_through_claude_runtime_env(monkeypatch):
    """Launcher should forward Claude runtime settings from the parent env to per-task agents."""

    monkeypatch.setenv("ARTIFACT_ROOT", "/app/artifacts")
    monkeypatch.setenv("REGISTRY_URL", "http://registry:9000")
    monkeypatch.setenv("AGENT_RUNTIME", "claude-code")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-from-config-env")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://anthropic.example.test")
    monkeypatch.setenv("ANTHROPIC_MODEL", "MiniMax-M2.7")

    launcher = Launcher(socket_path="/tmp/fake-docker.sock")
    requests = []

    def fake_request(method, path, payload=None):
        requests.append((method, path, payload))
        return {}

    monkeypatch.setattr(launcher, "_request", fake_request)
    monkeypatch.setattr(launcher, "resolve_host_path", lambda path: "/host/artifacts" if path == "/app/artifacts" else path)
    monkeypatch.setattr("framework.launcher.time.sleep", lambda _: None)

    agent = AgentDefinition(
        agent_id="office",
        name="Office Agent",
        description="Office",
        launch_spec={
            "image": "constellation-v2-office:latest",
            "port": 8060,
            "pass_through_env": [
                "AGENT_RUNTIME",
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_BASE_URL",
                "ANTHROPIC_MODEL",
            ],
        },
    )

    launcher.launch_instance(agent, "task-123")

    create_requests = [payload for method, path, payload in requests if method == "POST" and path.startswith("/v1.43/containers/create")]
    assert len(create_requests) == 1

    create_payload = create_requests[0]
    assert "AGENT_RUNTIME=claude-code" in create_payload["Env"]
    assert "ANTHROPIC_AUTH_TOKEN=token-from-config-env" in create_payload["Env"]
    assert "ANTHROPIC_BASE_URL=https://anthropic.example.test" in create_payload["Env"]
    assert "ANTHROPIC_MODEL=MiniMax-M2.7" in create_payload["Env"]


def test_resolve_container_path_maps_host_mounts(monkeypatch):
    """Launcher should translate host-visible paths back into the current container mount path."""

    launcher = Launcher(socket_path="/tmp/fake-docker.sock")
    monkeypatch.setattr(
        launcher,
        "_current_container_mounts",
        lambda: [
            {
                "Source": "/Users/test/project",
                "Destination": "/workspace",
            }
        ],
    )

    translated = launcher.resolve_container_path("/Users/test/project/tests/data/2026")

    assert translated == "/workspace/tests/data/2026"


def test_launch_instance_creates_task_dir_via_container_path_first(monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", "/app/artifacts")
    monkeypatch.setenv("REGISTRY_URL", "http://registry:9000")

    launcher = Launcher(socket_path="/tmp/fake-docker.sock")
    requests = []
    mkdir_calls = []

    def fake_request(method, path, payload=None):
        requests.append((method, path, payload))
        return {}

    def fake_makedirs(path, exist_ok=False):
        mkdir_calls.append(path)
        if str(path).startswith("/app/artifacts"):
            return None
        raise AssertionError(f"unexpected fallback makedirs path: {path}")

    monkeypatch.setattr(launcher, "_request", fake_request)
    monkeypatch.setattr(
        launcher,
        "resolve_host_path",
        lambda path: "/host/artifacts" if path == "/app/artifacts" else "/host/artifacts/task-123",
    )
    monkeypatch.setattr("framework.launcher.os.makedirs", fake_makedirs)
    monkeypatch.setattr("framework.launcher.time.sleep", lambda _: None)

    agent = AgentDefinition(
        agent_id="code-review",
        name="Code Review Agent",
        description="Code Review",
        launch_spec=LaunchSpec(image="constellation-v2-code-review:latest", port=8060),
    )

    launcher.launch_instance(agent, "task-123")

    assert mkdir_calls == ["/app/artifacts/task-123"]
    create_requests = [payload for method, path, payload in requests if method == "POST" and path.startswith("/v1.43/containers/create")]
    assert len(create_requests) == 1
    assert "/host/artifacts/task-123:/app/artifacts/task-123" in create_requests[0]["HostConfig"]["Binds"]
