"""Container runtime contract tests for agent images and dynamic launches."""

from __future__ import annotations

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_thin_dockerfiles_use_run_local_agent_ids() -> None:
    expected_cmds = {
        "agents/team_lead/Dockerfile": 'CMD ["python3", "scripts/run_local.py", "team-lead", "--port", "8030"]',
        "agents/code_review/Dockerfile": 'CMD ["python3", "scripts/run_local.py", "code-review"]',
        "agents/ui_design/Dockerfile": 'CMD ["python3", "scripts/run_local.py", "ui-design", "--port", "8040"]',
    }

    for relative_path, expected_cmd in expected_cmds.items():
        dockerfile = PROJECT_ROOT / relative_path
        assert expected_cmd in dockerfile.read_text(encoding="utf-8")


def test_per_task_agents_forward_copilot_byok_env() -> None:
    required_env = {
        "COPILOT_MODEL",
        "COPILOT_PROVIDER_API_KEY",
        "COPILOT_PROVIDER_BASE_URL",
        "COPILOT_PROVIDER_TYPE",
    }
    config_paths = [
        "agents/office/config.yaml",
        "agents/web_dev/config.yaml",
        "agents/code_review/config.yaml",
    ]

    for relative_path in config_paths:
        config = yaml.safe_load((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))
        pass_through_env = set(config["launch_spec"]["pass_through_env"])
        assert required_env <= pass_through_env
