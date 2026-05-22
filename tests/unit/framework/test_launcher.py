"""Unit tests for the per-task container launcher."""

from framework.agent import AgentDefinition, LaunchSpec
from framework.launcher import Launcher


def test_launch_instance_passes_artifact_root_env(monkeypatch):
    """Launcher should export ARTIFACT_ROOT when it bind-mounts the artifacts volume."""

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
    assert "/host/artifacts:/app/artifacts" in create_payload["HostConfig"]["Binds"]
    assert "ARTIFACT_ROOT=/app/artifacts" in create_payload["Env"]


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
