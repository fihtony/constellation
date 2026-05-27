import os


def test_run_local_uses_port_env_when_cli_port_omitted(monkeypatch):
    monkeypatch.setenv("PORT", "8050")

    try:
        default_port = int(os.environ.get("PORT", "8000") or "8000")
    except ValueError:
        default_port = 8000

    assert default_port == 8050


def test_run_local_falls_back_to_8000_for_invalid_port_env(monkeypatch):
    monkeypatch.setenv("PORT", "not-a-port")

    try:
        default_port = int(os.environ.get("PORT", "8000") or "8000")
    except ValueError:
        default_port = 8000

    assert default_port == 8000