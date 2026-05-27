"""Tests for the manual registry registration script."""

import json
import subprocess
import sys
from pathlib import Path

from scripts.register_agents import _build_registration_payload, _iter_agent_ids


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_build_registration_payload_uses_office_capabilities_and_launch_spec():
    payload = _build_registration_payload("office")

    assert payload["agentId"] == "office"
    assert payload["executionMode"] == "per-task"
    assert payload["capabilities"] == [
        "office.document.summarize",
        "office.data.analyze",
        "office.folder.organize",
    ]
    assert payload["launchSpec"]["image"] == "constellation-v2-office:latest"
    assert payload["launchSpec"]["port"] == 8060


def test_build_registration_payload_uses_web_dev_launch_spec():
    payload = _build_registration_payload("web-dev")

    assert payload["agentId"] == "web-dev"
    assert payload["executionMode"] == "per-task"
    assert payload["capabilities"] == ["web-dev.task.execute"]
    assert payload["launchSpec"]["image"] == "constellation-v2-web-dev:latest"
    assert payload["launchSpec"]["port"] == 8050


def test_build_registration_payload_uses_code_review_launch_spec():
    payload = _build_registration_payload("code-review")

    assert payload["agentId"] == "code-review"
    assert payload["executionMode"] == "per-task"
    assert payload["capabilities"] == ["review.code.check"]
    assert payload["launchSpec"]["image"] == "constellation-v2-code-review:latest"
    assert payload["launchSpec"]["port"] == 8060


def test_build_registration_payload_uses_team_lead_capabilities():
    payload = _build_registration_payload("team-lead")

    assert payload["agentId"] == "team-lead"
    assert payload["executionMode"] == "persistent"
    assert payload["capabilities"] == ["team-lead.task.analyze"]
    assert payload["cardUrl"] == "http://team-lead:8030/.well-known/agent-card.json"


def test_iter_agent_ids_skips_configs_without_capabilities():
    agent_ids = _iter_agent_ids()

    assert "office" in agent_ids
    assert "log-store" not in agent_ids


def test_register_agents_script_runs_directly_from_repo_root():
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "register_agents.py"),
            "--dry-run",
            "--agent",
            "office",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payloads = json.loads(result.stdout)
    assert payloads[0]["agentId"] == "office"
    assert payloads[0]["payload"]["capabilities"] == [
        "office.document.summarize",
        "office.data.analyze",
        "office.folder.organize",
    ]
