"""Workflow config loader — loads task workflow and rule configurations.

Provides utilities to load externalized workflow definitions, rules, and
definition-of-done from YAML config files.
"""
from __future__ import annotations

import os
from typing import Any

import yaml  # type: ignore[import-untyped]

_CONFIG_ROOT = os.path.join(os.path.dirname(__file__), "..", "config")


def _resolve_config_path(relative_path: str) -> str:
    """Resolve a config path relative to project root."""
    # Support both absolute and relative paths
    if os.path.isabs(relative_path):
        return relative_path
    # Try relative to project root
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.join(project_root, relative_path)
    if os.path.exists(candidate):
        return candidate
    # Fallback to config root
    return os.path.join(_CONFIG_ROOT, os.path.basename(relative_path))


def load_workflow_config(workflow_ref: str) -> dict[str, Any]:
    """Load a workflow YAML config file.

    Args:
        workflow_ref: Relative path like 'config/workflows/development_task.yaml'

    Returns:
        Parsed YAML dict, or empty dict if file not found.
    """
    path = _resolve_config_path(workflow_ref)
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}


def load_rules_config(rule_ref: str) -> dict[str, Any]:
    """Load a rules YAML config file.

    Args:
        rule_ref: Relative path like 'config/rules/development_standards.yaml'

    Returns:
        Parsed YAML dict, or empty dict if file not found.
    """
    path = _resolve_config_path(rule_ref)
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}


def load_definition_of_done(workflow_ref: str) -> dict[str, Any]:
    """Extract definition_of_done from a workflow config.

    Args:
        workflow_ref: Path to workflow YAML.

    Returns:
        The definition_of_done section, or empty dict.
    """
    config = load_workflow_config(workflow_ref)
    return config.get("definition_of_done", {})


def load_validation_gates(workflow_ref: str) -> dict[str, Any]:
    """Extract validation_gates from a workflow config.

    Args:
        workflow_ref: Path to workflow YAML.

    Returns:
        The validation_gates section, or empty dict.
    """
    config = load_workflow_config(workflow_ref)
    return config.get("validation_gates", {})


def load_limits(workflow_ref: str) -> dict[str, Any]:
    """Extract limits from a workflow config.

    Args:
        workflow_ref: Path to workflow YAML.

    Returns:
        The limits section, or empty dict.
    """
    config = load_workflow_config(workflow_ref)
    return config.get("limits", {})
