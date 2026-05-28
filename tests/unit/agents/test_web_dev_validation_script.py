"""Tests for Web Dev mandatory validation command selection."""

from __future__ import annotations

import json

from agents.web_dev.scripts.validate_project import _test_command


def test_vitest_test_command_does_not_duplicate_run_flag():
    package = {
        "scripts": {"test": "vitest --run"},
        "devDependencies": {"vitest": "^2.0.0"},
    }

    assert _test_command(package) == ["npm", "test"]


def test_vitest_test_command_adds_run_flag_when_missing():
    package = {
        "scripts": {"test": "vitest"},
        "devDependencies": {"vitest": "^2.0.0"},
    }

    assert _test_command(package) == ["npm", "test", "--", "--run"]


def test_jest_test_command_does_not_duplicate_run_in_band_flag():
    package = {
        "scripts": {"test": "jest --runInBand"},
        "devDependencies": {"jest": "^29.0.0"},
    }

    assert _test_command(package) == ["npm", "test"]


def test_validate_rejects_pass_with_no_tests(monkeypatch, tmp_path):
    from agents.web_dev.scripts import validate_project

    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "scripts": {
                    "build": "vite build",
                    "test": "vitest --run --passWithNoTests",
                },
                "devDependencies": {"vitest": "^1.0.0"},
            }
        ),
        encoding="utf-8",
    )

    def fake_run(command, cwd, timeout):
        if command == ["npm", "install"]:
            return {"command": command, "returncode": 0, "duration_seconds": 0.0, "output": "install ok"}
        if command == ["npm", "run", "build"]:
            return {"command": command, "returncode": 0, "duration_seconds": 0.0, "output": "build ok"}
        return {
            "command": command,
            "returncode": 0,
            "duration_seconds": 0.0,
            "output": "No test files found, exiting with code 0",
        }

    monkeypatch.setattr(validate_project, "_run", fake_run)

    summary = validate_project.validate(tmp_path)

    assert summary["install_ok"] is True
    assert summary["build_ok"] is True
    assert summary["test_ok"] is False
    assert "package.json test script must not use passWithNoTests" in summary["errors"]
    assert "no test files found" in summary["errors"]
    assert "test command failed" in summary["errors"]