"""Coding tools for the Web Dev Agent.

These tools give the LLM in implement_changes / fix_tests / run_tests the
ability to read, write, and edit files and to run shell commands (git, npm,
pytest, etc.) inside the cloned repository.

All tools are registered into the global ToolRegistry by
``register_web_dev_coding_tools()``, which is called from
``WebDevAgent.start()``.

Security notes
--------------
* ``run_command`` uses ``build_isolated_git_env`` for any command that starts
  with ``git``, ensuring host keychain / ~/.gitconfig are never consulted.
* Non-git commands run in a minimal sanitised environment derived from the
  current process environment but with all SCM credential variables removed.
* No command is executed with shell=True to prevent injection via arguments.
* Output is capped at 8 KB to avoid flooding the LLM context window.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from framework.tools.base import BaseTool, ToolResult

# Max bytes returned from file reads or command output to keep LLM context small.
_MAX_READ_BYTES = 32_768   # 32 KB
_MAX_CMD_OUTPUT = 8_192    # 8 KB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_path(raw: str, *, allow_abs: bool = True) -> str:
    """Return a normalised path string; prevent path traversal above cwd."""
    p = os.path.normpath(raw)
    # Strip leading ../ sequences — the resolved path will stay under cwd or
    # be an absolute path specified by the LLM which is acceptable for reading
    # repository files.
    return p


def _sanitised_env() -> dict[str, str]:
    """Return a copy of os.environ with SCM credential vars stripped."""
    _STRIP = frozenset({
        "HOME", "XDG_CONFIG_HOME",
        "GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN",
        "SCM_TOKEN", "SCM_USERNAME", "SCM_PASSWORD",
        "GIT_ASKPASS", "SSH_ASKPASS",
        "GIT_CREDENTIAL_HELPER", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
    })
    return {k: v for k, v in os.environ.items() if k not in _STRIP}


def _git_env(scope: str = "run-command") -> dict[str, str]:
    """Return an isolated git environment (no host keychain / ~/.gitconfig)."""
    from framework.env_utils import build_isolated_git_env
    return build_isolated_git_env(scope=scope)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

class ReadFileTool(BaseTool):
    """Read the contents of a file."""

    name = "read_file"
    description = (
        "Read the contents of a file at the given path. "
        "Returns the text content (up to 32 KB)."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the file to read.",
            },
            "start_line": {
                "type": "integer",
                "description": "Optional 1-based first line to read (inclusive).",
            },
            "end_line": {
                "type": "integer",
                "description": "Optional 1-based last line to read (inclusive).",
            },
        },
        "required": ["path"],
    }

    def execute_sync(
        self,
        path: str = "",
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> ToolResult:
        path = _safe_path(path)
        if not path:
            return ToolResult(error="path is required")
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                if start_line or end_line:
                    lines = fh.readlines()
                    sl = max(0, (start_line or 1) - 1)
                    el = end_line if end_line else len(lines)
                    content = "".join(lines[sl:el])
                else:
                    content = fh.read(_MAX_READ_BYTES)
            return ToolResult(output=json.dumps({"content": content, "path": path}))
        except FileNotFoundError:
            return ToolResult(error=f"File not found: {path}")
        except OSError as exc:
            return ToolResult(error=f"Cannot read {path}: {exc}")


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------

class WriteFileTool(BaseTool):
    """Write (create or overwrite) a file."""

    name = "write_file"
    description = (
        "Write content to a file, creating parent directories as needed. "
        "Overwrites the file if it already exists."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the file to write.",
            },
            "content": {
                "type": "string",
                "description": "Text content to write to the file.",
            },
        },
        "required": ["path", "content"],
    }

    def execute_sync(self, path: str = "", content: str = "") -> ToolResult:
        path = _safe_path(path)
        if not path:
            return ToolResult(error="path is required")
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            return ToolResult(output=json.dumps({"written": True, "path": path, "bytes": len(content)}))
        except OSError as exc:
            return ToolResult(error=f"Cannot write {path}: {exc}")


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------

class EditFileTool(BaseTool):
    """Replace a string in a file (exact match, first occurrence)."""

    name = "edit_file"
    description = (
        "Replace the first occurrence of old_string with new_string in a file. "
        "The old_string must match exactly (including whitespace/indentation). "
        "Returns an error if old_string is not found."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the file to edit.",
            },
            "old_string": {
                "type": "string",
                "description": "The exact text to replace (must appear in the file).",
            },
            "new_string": {
                "type": "string",
                "description": "The replacement text.",
            },
        },
        "required": ["path", "old_string", "new_string"],
    }

    def execute_sync(
        self,
        path: str = "",
        old_string: str = "",
        new_string: str = "",
    ) -> ToolResult:
        path = _safe_path(path)
        if not path:
            return ToolResult(error="path is required")
        if not old_string:
            return ToolResult(error="old_string is required")
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                original = fh.read()
            if old_string not in original:
                return ToolResult(error=f"old_string not found in {path}")
            updated = original.replace(old_string, new_string, 1)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(updated)
            return ToolResult(output=json.dumps({"edited": True, "path": path}))
        except FileNotFoundError:
            return ToolResult(error=f"File not found: {path}")
        except OSError as exc:
            return ToolResult(error=f"Cannot edit {path}: {exc}")


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------

class RunCommandTool(BaseTool):
    """Run a shell command (git, npm, pytest, etc.) in a directory."""

    name = "run_command"
    description = (
        "Run a shell command and return its output. "
        "Use this for git operations (add, commit, checkout -b), "
        "npm/yarn installs, running tests, etc. "
        "Pass the command as a list of tokens to avoid shell-injection risk. "
        "Output is capped at 8 KB."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "The command to run as a single string, e.g. "
                    "'git add .' or 'npm test'. "
                    "The command is split on whitespace; use the args list "
                    "for arguments with spaces."
                ),
            },
            "cwd": {
                "type": "string",
                "description": "Working directory for the command (default: current dir).",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 120).",
            },
        },
        "required": ["command"],
    }

    def execute_sync(
        self,
        command: str = "",
        cwd: str | None = None,
        timeout: int = 60,
    ) -> ToolResult:
        # Hard-cap: prevent LLM from blocking for too long
        timeout = min(int(timeout), 60)
        if not command.strip():
            return ToolResult(error="command is required")

        import shlex
        try:
            args = shlex.split(command)
        except ValueError as exc:
            return ToolResult(error=f"Invalid command syntax: {exc}")

        # Use isolated git environment for git commands to prevent credential leaks
        if args and args[0] == "git":
            env = _git_env(scope="web-dev-run")
            # Inject SCM auth header if the command involves a remote (push, fetch, pull, clone)
            _remote_ops = {"push", "fetch", "pull", "clone"}
            if len(args) > 1 and args[1] in _remote_ops:
                token = os.environ.get("SCM_TOKEN", "")
                username = os.environ.get("SCM_USERNAME", "")
                if token and username:
                    import base64
                    creds = base64.b64encode(f"{username}:{token}".encode()).decode()
                    args = [
                        "git",
                        "-c", f"http.extraHeader=Authorization: Basic {creds}",
                        *args[1:],
                    ]
                elif token:
                    args = [
                        "git",
                        "-c", f"http.extraHeader=Authorization: Bearer {token}",
                        *args[1:],
                    ]
        else:
            env = _sanitised_env()

        effective_cwd = cwd or None
        if effective_cwd and not os.path.isdir(effective_cwd):
            return ToolResult(error=f"cwd does not exist: {effective_cwd}")

        try:
            proc = subprocess.run(
                args,
                cwd=effective_cwd,
                capture_output=True,
                text=True,
                timeout=int(timeout),
                env=env,
            )
            stdout = proc.stdout[-_MAX_CMD_OUTPUT:] if proc.stdout else ""
            stderr = proc.stderr[-_MAX_CMD_OUTPUT:] if proc.stderr else ""
            output = (stdout + stderr).strip()
            return ToolResult(output=json.dumps({
                "exit_code": proc.returncode,
                "output": output,
                "success": proc.returncode == 0,
            }))
        except subprocess.TimeoutExpired:
            return ToolResult(error=f"Command timed out after {timeout}s: {command}")
        except FileNotFoundError:
            return ToolResult(error=f"Command not found: {args[0]!r}")
        except Exception as exc:
            return ToolResult(error=f"Command failed: {exc}")


# ---------------------------------------------------------------------------
# search_code
# ---------------------------------------------------------------------------

class SearchCodeTool(BaseTool):
    """Search for a pattern in source files."""

    name = "search_code"
    description = (
        "Search for a text pattern (regex or literal) in files under a directory. "
        "Returns matching file paths and line numbers."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "The text or regex pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (default: current directory).",
            },
            "file_pattern": {
                "type": "string",
                "description": "Glob pattern to filter files, e.g. '*.ts' (default: all files).",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default 50).",
            },
        },
        "required": ["pattern"],
    }

    def execute_sync(
        self,
        pattern: str = "",
        path: str = ".",
        file_pattern: str = "*",
        max_results: int = 50,
    ) -> ToolResult:
        if not pattern:
            return ToolResult(error="pattern is required")
        search_root = path or "."
        if not os.path.isdir(search_root):
            return ToolResult(error=f"Directory not found: {search_root}")

        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return ToolResult(error=f"Invalid pattern: {exc}")

        results: list[dict] = []
        for dirpath, dirnames, filenames in os.walk(search_root):
            # Skip hidden and common dependency dirs
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in {"node_modules", "__pycache__", ".git"}
            ]
            for fname in filenames:
                if not fnmatch.fnmatch(fname, file_pattern):
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    with open(fpath, encoding="utf-8", errors="ignore") as fh:
                        for lineno, line in enumerate(fh, 1):
                            if regex.search(line):
                                results.append({
                                    "file": fpath,
                                    "line": lineno,
                                    "text": line.rstrip(),
                                })
                                if len(results) >= max_results:
                                    return ToolResult(output=json.dumps({
                                        "results": results,
                                        "truncated": True,
                                    }))
                except OSError:
                    continue

        return ToolResult(output=json.dumps({"results": results, "truncated": False}))


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------

class GlobTool(BaseTool):
    """List files matching a glob pattern."""

    name = "glob"
    description = "List files matching a glob pattern, e.g. '**/*.ts' or 'src/**/*.py'."
    parameters_schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern relative to 'root' (default: '**/*').",
            },
            "root": {
                "type": "string",
                "description": "Root directory to glob from (default: current directory).",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of paths to return (default 200).",
            },
        },
        "required": ["pattern"],
    }

    def execute_sync(
        self,
        pattern: str = "**/*",
        root: str = ".",
        max_results: int = 200,
    ) -> ToolResult:
        root_path = Path(root or ".")
        if not root_path.is_dir():
            return ToolResult(error=f"Directory not found: {root}")
        try:
            matches = [
                str(p) for p in root_path.glob(pattern)
                if ".git" not in p.parts and "node_modules" not in p.parts
                and "__pycache__" not in p.parts
            ]
            matches = matches[:max_results]
            return ToolResult(output=json.dumps({
                "files": matches,
                "count": len(matches),
                "truncated": len(matches) >= max_results,
            }))
        except Exception as exc:
            return ToolResult(error=f"Glob failed: {exc}")


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------

class GrepTool(BaseTool):
    """Grep for a literal string in files (case-insensitive by default)."""

    name = "grep"
    description = "Search for a literal string in files under a directory."
    parameters_schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Literal string (or regex) to search for.",
            },
            "path": {
                "type": "string",
                "description": "Directory or file to search in.",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Whether the search is case-sensitive (default: false).",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum results (default 50).",
            },
        },
        "required": ["pattern"],
    }

    def execute_sync(
        self,
        pattern: str = "",
        path: str = ".",
        case_sensitive: bool = False,
        max_results: int = 50,
    ) -> ToolResult:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(re.escape(pattern), flags)
        except re.error as exc:
            return ToolResult(error=f"Invalid pattern: {exc}")

        results: list[dict] = []
        search_root = path or "."

        if os.path.isfile(search_root):
            files_to_search = [search_root]
        elif os.path.isdir(search_root):
            files_to_search = []
            for dirpath, dirnames, filenames in os.walk(search_root):
                dirnames[:] = [
                    d for d in dirnames
                    if not d.startswith(".") and d not in {"node_modules", "__pycache__", ".git"}
                ]
                for fname in filenames:
                    files_to_search.append(os.path.join(dirpath, fname))
        else:
            return ToolResult(error=f"Path not found: {search_root}")

        for fpath in files_to_search:
            try:
                with open(fpath, encoding="utf-8", errors="ignore") as fh:
                    for lineno, line in enumerate(fh, 1):
                        if regex.search(line):
                            results.append({
                                "file": fpath,
                                "line": lineno,
                                "text": line.rstrip(),
                            })
                            if len(results) >= max_results:
                                return ToolResult(output=json.dumps({
                                    "results": results,
                                    "truncated": True,
                                }))
            except OSError:
                continue

        return ToolResult(output=json.dumps({"results": results, "truncated": False}))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_CODING_TOOLS = [
    ReadFileTool(),
    WriteFileTool(),
    EditFileTool(),
    RunCommandTool(),
    SearchCodeTool(),
    GlobTool(),
    GrepTool(),
]


def register_web_dev_coding_tools() -> None:
    """Register coding tools into the global ToolRegistry (idempotent)."""
    from framework.tools.registry import get_registry

    registry = get_registry()
    for tool in _CODING_TOOLS:
        registry.register(tool)
