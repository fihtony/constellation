"""Security sandbox for Connect Agent runtime.

Provides path-jail enforcement, command safety filtering, and audit logging.
All file and shell operations flow through this module before execution.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import time
from pathlib import Path

_DEFAULT_SENSITIVE_PATTERNS: list[str] = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    ".git/config",
    ".git/credentials",
    "**/.ssh/**",
    "**/secrets/**",
]

_DANGEROUS_COMMAND_PATTERNS: list[str] = [
    "rm -rf /",
    "sudo ",
    "shutdown",
    "reboot",
    "mkfs",
    "dd if=",
    ":(){ ",
    "chmod -R 777 /",
    "> /dev/",
    "chown root",
    "> ~/.ssh/",
    "> /etc/",
]

_PIPE_SHELL_RE = re.compile(
    r"(curl|wget)\s+.*\|\s*(bash|sh|zsh|python|perl|ruby)",
    re.IGNORECASE,
)

MAX_PATH_LENGTH = 4096
MAX_COMMAND_LENGTH = 8192
MAX_FILE_SIZE = 5 * 1024 * 1024
MAX_OUTPUT_SIZE = 50 * 1024

_audit_chain: list[str] = []


class SecurityError(Exception):
    """Raised when a security policy is violated."""


def safe_path(
    p: str,
    sandbox_root: str | Path,
    *,
    allow_roots: list[str] | None = None,
    sensitive_patterns: list[str] | None = None,
    check_sensitive: bool = True,
) -> Path:
    if not p or len(p) > MAX_PATH_LENGTH:
        raise SecurityError(f"Path rejected (empty or too long, len={len(p) if p else 0}).")

    sandbox = Path(sandbox_root).resolve()
    roots = [sandbox] + [Path(r).resolve() for r in (allow_roots or [])]

    target = Path(p)
    if not target.is_absolute():
        target = sandbox / target
    resolved = target.resolve()

    inside = any(resolved == root or _is_subpath(resolved, root) for root in roots)
    if not inside:
        raise SecurityError(f"Path escapes sandbox: {p}")

    if check_sensitive:
        patterns = sensitive_patterns or _DEFAULT_SENSITIVE_PATTERNS
        rel = str(resolved.relative_to(sandbox)) if _is_subpath(resolved, sandbox) else resolved.name
        for pattern in patterns:
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(resolved.name, pattern):
                raise SecurityError(f"Access denied — sensitive path: {p}")

    return resolved


def _is_subpath(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def check_command_safety(
    command: str,
    *,
    extra_deny_patterns: list[str] | None = None,
) -> None:
    if not command or len(command) > MAX_COMMAND_LENGTH:
        raise SecurityError(f"Command rejected (empty or too long, len={len(command) if command else 0}).")

    lowered = command.lower().strip()
    for pattern in _DANGEROUS_COMMAND_PATTERNS + (extra_deny_patterns or []):
        if pattern.lower() in lowered:
            raise SecurityError(f"Dangerous command blocked: matches '{pattern}'")

    if _PIPE_SHELL_RE.search(command):
        raise SecurityError("Pipe-to-shell execution blocked.")


def check_regex_safety(pattern: str, max_length: int = 500) -> None:
    if len(pattern) > max_length:
        raise SecurityError(f"Regex too long ({len(pattern)} chars, max {max_length}).")
    if re.search(r"\(.+\)\{.*\}\{", pattern) or re.search(r"(\.\*){3,}", pattern):
        raise SecurityError("Regex contains nested quantifiers — potential ReDoS.")


def truncate_output(text: str, max_size: int = MAX_OUTPUT_SIZE) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_size:
        return text
    truncated = encoded[:max_size].decode("utf-8", errors="replace")
    return truncated + f"\n\n... [output truncated at {max_size} bytes]"


def is_binary_file(path: str | Path, sample_size: int = 8192) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(sample_size)
    except OSError:
        return False


def audit_log(event: str, **kwargs: object) -> None:
    prev_hash = _audit_chain[-1] if _audit_chain else "genesis"
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "event": event,
        **{k: _safe_serialize(v) for k, v in kwargs.items()},
    }
    raw = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    entry_hash = hashlib.sha256(f"{prev_hash}|{raw}".encode()).hexdigest()[:16]
    _audit_chain.append(entry_hash)
    entry["_chain"] = entry_hash
    print(f"[connect-agent][audit] {json.dumps(entry, ensure_ascii=False)}")


def _safe_serialize(value: object) -> object:
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, dict):
        return {k: _safe_serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_serialize(v) for v in value]
    return str(value)


_DEFAULT_ENV_PASSTHROUGH = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "TERM",
    "PYTHONPATH", "NODE_PATH", "JAVA_HOME", "PYTHONUNBUFFERED",
})


# ---------------------------------------------------------------------------
# SecretRedactor — redact sensitive tokens from tool output before returning
# to the model context.  Patterns cover common API key formats.
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    # GitHub tokens
    re.compile(r"(ghp_[A-Za-z0-9]{36,})"),
    re.compile(r"(gho_[A-Za-z0-9]{36,})"),
    re.compile(r"(ghs_[A-Za-z0-9]{36,})"),
    re.compile(r"(ghr_[A-Za-z0-9]{36,})"),
    re.compile(r"(github_pat_[A-Za-z0-9_]{20,})"),
    # AWS
    re.compile(r"(AKIA[A-Z0-9]{16})"),
    re.compile(r"(?i)(aws_secret_access_key\s*[:=]\s*)[A-Za-z0-9/+=]{30,}"),
    # Generic API keys
    re.compile(r"(sk-[A-Za-z0-9]{20,})"),
    re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9\-_.~+/]{20,}"),
    # Private keys
    re.compile(r"(-----BEGIN\s+(?:RSA\s+)?PRIVATE KEY-----[\s\S]*?-----END\s+(?:RSA\s+)?PRIVATE KEY-----)"),
    # Jira / Atlassian tokens (base-64-ish long strings after auth headers)
    re.compile(r"(?i)((?:jira|atlassian)[_-]?token\s*[:=]\s*)[A-Za-z0-9/+=]{20,}"),
]

_REDACTED = "[REDACTED]"


def redact_secrets(text: str) -> str:
    """Scan *text* and replace detected secrets with ``[REDACTED]``."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(0)[:4] + _REDACTED if len(m.groups()) == 1
                           else m.group(1) + _REDACTED, text)
    return text


def build_sandbox_env(
    *,
    cwd: str | None = None,
    extra_passthrough: list[str] | None = None,
) -> dict[str, str]:
    allowed = _DEFAULT_ENV_PASSTHROUGH | set(extra_passthrough or [])
    env: dict[str, str] = {}
    for key in allowed:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    if cwd:
        env["HOME"] = cwd
    return env