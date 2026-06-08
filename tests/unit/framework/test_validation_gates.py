"""Unit tests for deterministic validation gates."""

from __future__ import annotations

import subprocess

from framework.validation_gates import validate_files_changed, validate_self_assessment


def _git(repo_path, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_validate_files_changed_detects_committed_branch_changes(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "user.email", "test@example.com")
    _git(repo_path, "config", "user.name", "Test User")
    (repo_path / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo_path, "add", "README.md")
    _git(repo_path, "commit", "-m", "initial")
    _git(repo_path, "checkout", "-b", "feature/test")
    (repo_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    _git(repo_path, "add", "app.py")
    _git(repo_path, "commit", "-m", "add app")

    result = validate_files_changed(str(repo_path))

    assert result.passed is True
    assert result.details["base_ref"] == "main"
    assert result.details["committed_files"] == ["app.py"]


def test_validate_files_changed_fails_clean_base_branch(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "user.email", "test@example.com")
    _git(repo_path, "config", "user.name", "Test User")
    (repo_path / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo_path, "add", "README.md")
    _git(repo_path, "commit", "-m", "initial")

    result = validate_files_changed(str(repo_path))

    assert result.passed is False
    assert "No file changes detected" in result.feedback


def test_validate_self_assessment_rejects_pass_when_self_review_issues_exist():
    result = validate_self_assessment(
        {
            "score": 0.95,
            "verdict": "pass",
            "criteria_checks": [],
            "self_review_issues": [
                {
                    "severity": "high",
                    "file": "src/app.py",
                    "line": 11,
                    "message": "Blocking issue.",
                    "blocking": True,
                }
            ],
        },
        acceptance_criteria_count=0,
    )

    assert result.passed is False
    assert "self_review_issues is non-empty" in result.feedback
