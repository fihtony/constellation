"""Tests for Web Dev mandatory validation command selection."""

from __future__ import annotations

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