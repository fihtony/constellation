"""Minimum-privilege policy engine for Connect Agent runtime."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PolicyProfile:
    name: str = "workspace-write"
    allow_tools: list[str] = field(default_factory=lambda: ["*"])
    deny_tools: list[str] = field(default_factory=list)
    allow_roots: list[str] = field(default_factory=list)
    sensitive_path_deny_list: list[str] = field(default_factory=list)
    allow_network: bool = False
    allowed_domains: list[str] = field(default_factory=list)
    denied_addresses: list[str] = field(
        default_factory=lambda: ["169.254.169.254", "100.100.100.200", "metadata.google.internal"]
    )
    allow_mcp_servers: list[str] = field(default_factory=list)
    require_approval: bool = False
    max_turns: int = 75
    max_timeout_seconds: int = 1800
    allow_subagent: bool = True
    subagent_profile: str | None = None
    bash_deny_patterns: list[str] = field(default_factory=list)
    bash_max_timeout: int = 600
    bash_env_passthrough: list[str] = field(
        default_factory=lambda: ["PATH", "HOME", "LANG", "LC_ALL", "TERM", "PYTHONPATH", "NODE_PATH"]
    )


@dataclass
class GlobalLimits:
    max_parallel_readonly_tools: int = 4
    max_background_jobs: int = 2
    artifact_quota_mb: int = 500
    subprocess_memory_mb: int = 512
    subprocess_max_pids: int = 64
    max_open_files: int = 256
    total_token_budget: int = 500_000
    per_turn_max_output_tokens: int = 4096
    context_window_tokens: int = 128_000
    token_warning_threshold: float = 0.8
    audit_log_enabled: bool = True
    secret_redaction_enabled: bool = True
    verifier_required: bool = True
    checkpoint_enabled: bool = True


@dataclass
class PolicyConfig:
    default_profile: str = "workspace-write"
    profiles: dict[str, PolicyProfile] = field(default_factory=dict)
    global_limits: GlobalLimits = field(default_factory=GlobalLimits)


_CORE_TOOL_NAMES = [
    "read_file",
    "glob",
    "grep",
    "load_skill",
    "list_skills",
    "todo_write",
    "report_progress",
    # canonical local-file aliases (same sandbox enforcement)
    "read_local_file",
    "write_local_file",
    "edit_local_file",
    "list_local_dir",
    "search_local_files",
]

_WRITE_TOOL_NAMES = [
    "write_file",
    "edit_file",
    "bash",
    "subagent",
    "compress",
]

# Orchestration and control-plane tools — used by Compass, Team Lead, and any
# agent that dispatches sub-tasks or manages task lifecycle.
_CONTROL_TOOL_NAMES = [
    # Task lifecycle
    "complete_current_task",
    "fail_current_task",
    "request_user_input",
    "get_task_context",
    "get_agent_runtime_status",
    # Agent dispatch and wait
    "dispatch_agent_task",
    "wait_for_agent_task",
    "ack_agent_task",
    "launch_per_task_agent",
    # Registry / discovery
    "registry_query",
    "list_available_agents",
    "check_agent_status",
    # Office path validation and bind-mount helper
    "validate_office_paths",
    # Task-card evidence aggregation
    "aggregate_task_card",
    "derive_user_facing_status",
]

_DOMAIN_TOOL_NAMES = [
    "jira_get_ticket",
    "jira_add_comment",
    "scm_create_branch",
    "scm_push_files",
    "scm_create_pr",
    "design_fetch_figma_screen",
    "design_fetch_stitch_screen",
]

# Document-format readers — PDF, DOCX, PPTX, XLSX.
_DOCUMENT_TOOL_NAMES = [
    "read_pdf",
    "read_docx",
    "read_pptx",
    "read_xlsx",
]

_BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "workspace-read": {
        "allow_tools": _CORE_TOOL_NAMES,
        "allow_network": False,
        "max_turns": 30,
        "max_timeout_seconds": 600,
    },
    "read-only": {
        "allow_tools": _CORE_TOOL_NAMES,
        "allow_network": False,
        "max_turns": 30,
        "max_timeout_seconds": 600,
    },
    "workspace-write": {
        "allow_tools": _CORE_TOOL_NAMES + _WRITE_TOOL_NAMES + _CONTROL_TOOL_NAMES + _DOCUMENT_TOOL_NAMES,
        "sensitive_path_deny_list": [
            ".env", ".env.*", "*.pem", "*.key", "id_rsa*",
            ".git/config", ".git/credentials", "**/.ssh/**", "**/secrets/**",
        ],
        "bash_deny_patterns": [
            "rm -rf /", "sudo ", "shutdown", "reboot", "mkfs",
            "dd if=", ":(){ ", "chmod -R 777 /",
            "curl *|* bash", "curl *|* sh", "wget *|* bash", "wget *|* sh",
            "> /dev/", "chown root", "> ~/.ssh/", "> /etc/",
        ],
        "allow_network": False,
        "max_turns": 75,
        "max_timeout_seconds": 1800,
        "subagent_profile": "workspace-write",
    },
    "design-to-code": {
        "allow_tools": _CORE_TOOL_NAMES + _WRITE_TOOL_NAMES + _CONTROL_TOOL_NAMES + _DOCUMENT_TOOL_NAMES,
        "sensitive_path_deny_list": [
            ".env", ".env.*", "*.pem", "*.key", "id_rsa*",
            ".git/config", ".git/credentials", "**/.ssh/**", "**/secrets/**",
        ],
        "bash_deny_patterns": [
            "rm -rf /", "sudo ", "shutdown", "reboot", "mkfs",
            "dd if=", ":(){ ", "chmod -R 777 /",
            "> /dev/", "chown root", "> ~/.ssh/", "> /etc/",
        ],
        "allow_network": False,
        "max_turns": 100,
        "max_timeout_seconds": 3600,
        "bash_max_timeout": 600,
        "subagent_profile": "workspace-write",
    },
    "integration-call": {
        "allow_tools": _CORE_TOOL_NAMES + _WRITE_TOOL_NAMES + _CONTROL_TOOL_NAMES + _DOCUMENT_TOOL_NAMES + _DOMAIN_TOOL_NAMES,
        "sensitive_path_deny_list": [
            ".env", ".env.*", "*.pem", "*.key", "id_rsa*",
            ".git/config", ".git/credentials", "**/.ssh/**", "**/secrets/**",
        ],
        "bash_deny_patterns": [
            "rm -rf /", "sudo ", "shutdown", "reboot", "mkfs",
            "dd if=", ":(){ ", "chmod -R 777 /",
            "> /dev/", "chown root", "> ~/.ssh/", "> /etc/",
        ],
        "allow_network": True,
        "allowed_domains": ["localhost", "api.githubcopilot.com"],
        "allow_mcp_servers": ["jira-mcp", "github-mcp", "constellation"],
        "max_turns": 80,
        "max_timeout_seconds": 3600,
        "subagent_profile": "workspace-write",
    },
    "privileged": {
        "allow_tools": ["*"],
        "allow_network": True,
        "allow_mcp_servers": ["*"],
        "require_approval": True,
        "max_turns": 100,
        "max_timeout_seconds": 3600,
        "subagent_profile": "workspace-write",
    },
}


def load_policy(policy_path: str | None = None) -> PolicyConfig:
    path = policy_path or os.environ.get("CONNECT_AGENT_POLICY_FILE", "")
    if path and os.path.isfile(path):
        return _parse_policy_file(path)
    return _default_policy()


def _default_policy() -> PolicyConfig:
    config = PolicyConfig()
    for name, overrides in _BUILTIN_PROFILES.items():
        profile = PolicyProfile(name=name)
        for key, value in overrides.items():
            if hasattr(profile, key):
                setattr(profile, key, list(value) if isinstance(value, list) else value)
        config.profiles[name] = profile
    return config


def _parse_policy_file(path: str) -> PolicyConfig:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)

    config = PolicyConfig(default_profile=raw.get("defaultProfile", "workspace-write"))

    global_limits = GlobalLimits()
    field_map = {
        "maxParallelReadonlyTools": "max_parallel_readonly_tools",
        "maxBackgroundJobs": "max_background_jobs",
        "artifactQuotaMb": "artifact_quota_mb",
        "subprocessMemoryMb": "subprocess_memory_mb",
        "subprocessMaxPids": "subprocess_max_pids",
        "maxOpenFiles": "max_open_files",
        "totalTokenBudget": "total_token_budget",
        "perTurnMaxOutputTokens": "per_turn_max_output_tokens",
        "contextWindowTokens": "context_window_tokens",
        "tokenWarningThreshold": "token_warning_threshold",
        "auditLogEnabled": "audit_log_enabled",
        "secretRedactionEnabled": "secret_redaction_enabled",
        "verifierRequired": "verifier_required",
        "checkpointEnabled": "checkpoint_enabled",
    }
    for json_key, attr in field_map.items():
        if json_key in raw.get("globalLimits", {}):
            setattr(global_limits, attr, raw["globalLimits"][json_key])
    config.global_limits = global_limits

    profiles_raw = raw.get("profiles", {})
    for name, prof_raw in profiles_raw.items():
        config.profiles[name] = _parse_profile(name, prof_raw, profiles_raw)
    return config


def _parse_profile(name: str, raw: dict[str, Any], all_profiles: dict[str, Any]) -> PolicyProfile:
    profile = PolicyProfile(name=name)
    parent_name = raw.get("extends")
    if parent_name and parent_name in all_profiles:
        parent = _parse_profile(parent_name, all_profiles[parent_name], all_profiles)
        for attr in (
            "allow_tools", "deny_tools", "allow_roots", "sensitive_path_deny_list",
            "allow_network", "allowed_domains", "denied_addresses", "allow_mcp_servers",
            "require_approval", "max_turns", "max_timeout_seconds", "allow_subagent",
            "subagent_profile", "bash_deny_patterns", "bash_max_timeout", "bash_env_passthrough",
        ):
            value = getattr(parent, attr)
            setattr(profile, attr, list(value) if isinstance(value, list) else value)

    field_map = {
        "allowTools": "allow_tools",
        "denyTools": "deny_tools",
        "allowRoots": "allow_roots",
        "sensitivePathDenyList": "sensitive_path_deny_list",
        "allowNetwork": "allow_network",
        "allowedDomains": "allowed_domains",
        "deniedAddresses": "denied_addresses",
        "allowMcpServers": "allow_mcp_servers",
        "requireApproval": "require_approval",
        "maxTurns": "max_turns",
        "maxTimeoutSeconds": "max_timeout_seconds",
        "allowSubagent": "allow_subagent",
        "subagentProfile": "subagent_profile",
    }
    for json_key, attr in field_map.items():
        if json_key not in raw:
            continue
        incoming = raw[json_key]
        current = getattr(profile, attr)
        if isinstance(current, list) and isinstance(incoming, list):
            merged = list(current)
            for item in incoming:
                if item not in merged:
                    merged.append(item)
            setattr(profile, attr, merged)
        else:
            setattr(profile, attr, incoming)

    bash_raw = raw.get("bash", {})
    if "denyPatterns" in bash_raw:
        profile.bash_deny_patterns = list(bash_raw["denyPatterns"])
    if "maxTimeoutSeconds" in bash_raw:
        profile.bash_max_timeout = bash_raw["maxTimeoutSeconds"]
    if "allowEnvPassthrough" in bash_raw:
        profile.bash_env_passthrough = list(bash_raw["allowEnvPassthrough"])

    return profile


def resolve_profile(config: PolicyConfig, profile_name: str | None = None) -> PolicyProfile:
    name = profile_name or config.default_profile
    if name == "read-only" and "workspace-read" in config.profiles:
        name = "workspace-read"
    if name in config.profiles:
        return config.profiles[name]
    if config.default_profile in config.profiles:
        return config.profiles[config.default_profile]
    if config.profiles:
        return next(iter(config.profiles.values()))
    return PolicyProfile()


def resolve_profile_for_session(
    config: PolicyConfig,
    profile_name: str | None = None,
    *,
    requested_tools: list[str] | None = None,
    wants_mcp: bool = False,
) -> PolicyProfile:
    if profile_name:
        return resolve_profile(config, profile_name)

    profile = resolve_profile(config)
    integration_profile = config.profiles.get("integration-call")
    if wants_mcp and integration_profile is not None:
        return integration_profile

    for tool_name in requested_tools or []:
        if is_tool_allowed(profile, tool_name):
            continue
        if integration_profile is not None and is_tool_allowed(integration_profile, tool_name):
            return integration_profile
    return profile


def is_tool_allowed(profile: PolicyProfile, tool_name: str) -> bool:
    if tool_name in profile.deny_tools:
        return False
    if "*" in profile.allow_tools:
        return True
    return tool_name in profile.allow_tools


def resolve_tool_names(
    profile: PolicyProfile,
    available_tool_names: list[str],
    *,
    requested_tools: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
) -> list[str]:
    ordered_available = list(dict.fromkeys(available_tool_names))
    allowed_by_profile = [name for name in ordered_available if is_tool_allowed(profile, name)]

    resolved = allowed_by_profile
    if requested_tools:
        requested = set(requested_tools)
        resolved = [name for name in ordered_available if name in requested and name in allowed_by_profile]
    if allowed_tools:
        allowed = set(allowed_tools)
        resolved = [name for name in resolved if name in allowed]

    denied = set(profile.deny_tools) | set(disallowed_tools or [])
    return [name for name in resolved if name not in denied]


def expand_sandbox_roots(
    profile: PolicyProfile,
    variables: dict[str, str] | None = None,
) -> list[str]:
    env = dict(os.environ)
    env.update(variables or {})
    expanded: list[str] = []
    for root in profile.allow_roots:
        result = root
        for var_name, var_value in env.items():
            result = result.replace(f"${{{var_name}}}", var_value)
        expanded.append(result)
    return expanded