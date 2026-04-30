"""General-purpose coding tools for the Connect Agent runtime.

Provides: bash, read_file, write_file, edit_file, glob, grep.
All tools follow the ConstellationTool pattern and self-register on import.
"""

from __future__ import annotations

import fnmatch
import glob as _glob_mod
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import register_tool
from common.runtime.connect_agent.sandbox import (
    SecurityError,
    audit_log,
    build_sandbox_env,
    check_command_safety,
    check_regex_safety,
    is_binary_file,
    safe_path,
    truncate_output,
    MAX_FILE_SIZE,
    MAX_OUTPUT_SIZE,
)

# ---------------------------------------------------------------------------
# Shared state — set by the agent loop before tool execution
# ---------------------------------------------------------------------------
_sandbox_root: str = os.getcwd()
_allow_roots: list[str] = []
_sensitive_patterns: list[str] = []
_bash_deny_patterns: list[str] = []
_bash_env_passthrough: list[str] = []


def configure_coding_tools(
    *,
    sandbox_root: str,
    allow_roots: list[str] | None = None,
    sensitive_patterns: list[str] | None = None,
    bash_deny_patterns: list[str] | None = None,
    bash_env_passthrough: list[str] | None = None,
) -> None:
    """Configure sandbox boundaries for all coding tools.

    Called once by the ConnectAgentAdapter before the agent loop starts.
    """
    global _sandbox_root, _allow_roots, _sensitive_patterns
    global _bash_deny_patterns, _bash_env_passthrough
    _sandbox_root = sandbox_root
    _allow_roots = allow_roots or []
    _sensitive_patterns = sensitive_patterns or []
    _bash_deny_patterns = bash_deny_patterns or []
    _bash_env_passthrough = bash_env_passthrough or []


def _resolve(p: str, *, check_sensitive: bool = True) -> Path:
    """Resolve a path through the sandbox jail."""
    return safe_path(
        p,
        _sandbox_root,
        allow_roots=_allow_roots,
        sensitive_patterns=_sensitive_patterns if check_sensitive else None,
        check_sensitive=check_sensitive,
    )


# ===================================================================
# bash
# ===================================================================

class BashTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="bash",
            description=(
                "Execute a shell command in the sandbox working directory. "
                "Output is captured and truncated to 50 KB. "
                "Dangerous commands are blocked."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 120, max 600).",
                    },
                },
                "required": ["command"],
            },
        )

    def execute(self, args: dict) -> dict:
        command = args.get("command", "")
        timeout = min(int(args.get("timeout", 120)), 600)
        start = time.time()

        try:
            check_command_safety(command, extra_deny_patterns=_bash_deny_patterns)
        except SecurityError as exc:
            audit_log("BASH_BLOCKED", command=command[:200], reason=str(exc))
            return self.error(f"Command blocked: {exc}")

        env = build_sandbox_env(cwd=_sandbox_root, extra_passthrough=_bash_env_passthrough)
        try:
            result = subprocess.run(
                ["bash", "-c", command],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=_sandbox_root,
                env=env,
            )
            elapsed = round(time.time() - start, 2)
            output = result.stdout + result.stderr
            output = truncate_output(output)

            audit_log(
                "BASH_EXEC",
                command=command[:200],
                exit_code=result.returncode,
                elapsed_s=elapsed,
            )

            header = f"Exit code: {result.returncode} | {elapsed}s\n"
            return self.ok(header + output)

        except subprocess.TimeoutExpired:
            audit_log("BASH_TIMEOUT", command=command[:200], timeout=timeout)
            return self.error(f"Command timed out after {timeout}s.")
        except OSError as exc:
            audit_log("BASH_ERROR", command=command[:200], error=str(exc))
            return self.error(f"Command execution failed: {exc}")


# ===================================================================
# read_file
# ===================================================================

class ReadFileTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="read_file",
            description=(
                "Read a file from the workspace. Returns content with line numbers. "
                "Use offset and limit for large files."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path (relative to workspace root).",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Start reading from this line (1-based, default 1).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to return (default 2000).",
                    },
                },
                "required": ["path"],
            },
        )

    def execute(self, args: dict) -> dict:
        path_str = args.get("path", "")
        offset = max(int(args.get("offset", 1)), 1)
        limit = min(int(args.get("limit", 2000)), 10000)

        try:
            resolved = _resolve(path_str)
        except SecurityError as exc:
            return self.error(str(exc))

        if not resolved.is_file():
            return self.error(f"File not found: {path_str}")

        if is_binary_file(resolved):
            return self.error(f"Binary file — cannot display: {path_str}")

        file_size = resolved.stat().st_size
        if file_size > MAX_FILE_SIZE and offset == 1 and limit >= 2000:
            return self.error(
                f"File too large ({file_size} bytes). Use offset/limit to read specific ranges."
            )

        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError as exc:
            return self.error(f"Cannot read file: {exc}")

        total = len(lines)
        start_idx = offset - 1
        end_idx = min(start_idx + limit, total)
        selected = lines[start_idx:end_idx]

        numbered = []
        for i, line in enumerate(selected, start=offset):
            numbered.append(f"{i:>6}  {line.rstrip()}")
        content = "\n".join(numbered)

        header = f"File: {path_str} | Lines {offset}-{end_idx} of {total}\n"
        output = header + truncate_output(content)

        audit_log("READ_FILE", path=path_str, lines=f"{offset}-{end_idx}/{total}")
        return self.ok(output)


# ===================================================================
# write_file
# ===================================================================

class WriteFileTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="write_file",
            description=(
                "Write content to a file. Creates parent directories if needed. "
                "Overwrites existing content."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path (relative to workspace root).",
                    },
                    "content": {
                        "type": "string",
                        "description": "The full file content to write.",
                    },
                },
                "required": ["path", "content"],
            },
        )

    def execute(self, args: dict) -> dict:
        path_str = args.get("path", "")
        content = args.get("content", "")

        if len(content.encode("utf-8")) > MAX_FILE_SIZE:
            return self.error(f"Content too large (max {MAX_FILE_SIZE} bytes).")

        try:
            resolved = _resolve(path_str, check_sensitive=True)
        except SecurityError as exc:
            return self.error(str(exc))

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: write to temp file then rename
            fd, tmp_path = tempfile.mkstemp(
                dir=str(resolved.parent),
                prefix=".write_",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(content)
                os.replace(tmp_path, str(resolved))
            except Exception:
                os.unlink(tmp_path)
                raise
        except SecurityError as exc:
            return self.error(str(exc))
        except OSError as exc:
            return self.error(f"Write failed: {exc}")

        byte_count = len(content.encode("utf-8"))
        audit_log("WRITE_FILE", path=path_str, bytes=byte_count)
        return self.ok(f"Wrote {byte_count} bytes to {path_str}")


# ===================================================================
# edit_file
# ===================================================================

class EditFileTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="edit_file",
            description=(
                "Replace an exact text occurrence in a file. "
                "old_text must appear exactly once in the file. "
                "Uses atomic write (temp + rename)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path (relative to workspace root).",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "The exact text to find (must appear exactly once).",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The replacement text.",
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
        )

    def execute(self, args: dict) -> dict:
        path_str = args.get("path", "")
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")

        if not old_text:
            return self.error("old_text must not be empty.")

        try:
            resolved = _resolve(path_str, check_sensitive=True)
        except SecurityError as exc:
            return self.error(str(exc))

        if not resolved.is_file():
            return self.error(f"File not found: {path_str}")

        try:
            original = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            return self.error(f"Cannot read file: {exc}")

        count = original.count(old_text)
        if count == 0:
            return self.error("old_text not found in file.")
        if count > 1:
            return self.error(f"old_text found {count} times — must appear exactly once.")

        new_content = original.replace(old_text, new_text, 1)

        # Atomic write
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(resolved.parent),
                prefix=".edit_",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(new_content)
                os.replace(tmp_path, str(resolved))
            except Exception:
                os.unlink(tmp_path)
                raise
        except OSError as exc:
            return self.error(f"Write failed: {exc}")

        # Brief diff summary
        old_lines = old_text.count("\n") + 1
        new_lines = new_text.count("\n") + 1
        audit_log("EDIT_FILE", path=path_str, old_lines=old_lines, new_lines=new_lines)
        return self.ok(
            f"Edited {path_str}: replaced {old_lines} line(s) with {new_lines} line(s)."
        )


# ===================================================================
# glob
# ===================================================================

class GlobTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="glob",
            description=(
                "Search for files matching a glob pattern in the workspace. "
                "Returns a list of matching file paths."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern (e.g., '**/*.py', 'src/**/*.ts').",
                    },
                    "path": {
                        "type": "string",
                        "description": "Base directory (default: workspace root).",
                    },
                },
                "required": ["pattern"],
            },
        )

    # Directories to always skip
    _SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox"}

    def execute(self, args: dict) -> dict:
        pattern = args.get("pattern", "")
        base = args.get("path", "")

        base_dir = _sandbox_root
        if base:
            try:
                base_dir = str(_resolve(base, check_sensitive=False))
            except SecurityError as exc:
                return self.error(str(exc))

        full_pattern = os.path.join(base_dir, pattern)
        matches: list[str] = []
        try:
            for p in _glob_mod.iglob(full_pattern, recursive=True):
                # Skip excluded directories
                parts = Path(p).parts
                if any(part in self._SKIP_DIRS for part in parts):
                    continue
                # Return relative path
                try:
                    rel = os.path.relpath(p, _sandbox_root)
                except ValueError:
                    rel = p
                matches.append(rel)
                if len(matches) >= 1000:
                    break
        except OSError as exc:
            return self.error(f"Glob failed: {exc}")

        if not matches:
            return self.ok(f"No files matching '{pattern}'.")

        result = "\n".join(sorted(matches))
        return self.ok(f"Found {len(matches)} file(s):\n{result}")


# ===================================================================
# grep
# ===================================================================

class GrepTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="grep",
            description=(
                "Search for text or regex patterns in workspace files. "
                "Returns matching lines with file paths and line numbers."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Search pattern (plain text or regex).",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file to search (default: workspace root).",
                    },
                    "include": {
                        "type": "string",
                        "description": "Glob pattern for files to include (e.g., '*.py').",
                    },
                    "is_regex": {
                        "type": "boolean",
                        "description": "Treat pattern as regex (default: false).",
                    },
                },
                "required": ["pattern"],
            },
        )

    _SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox"}
    _MAX_RESULTS = 500

    def execute(self, args: dict) -> dict:
        pattern = args.get("pattern", "")
        base = args.get("path", "")
        include = args.get("include", "")
        is_regex = args.get("is_regex", False)

        if not pattern:
            return self.error("Pattern must not be empty.")

        if is_regex:
            try:
                check_regex_safety(pattern)
                compiled = re.compile(pattern, re.IGNORECASE)
            except (SecurityError, re.error) as exc:
                return self.error(f"Invalid regex: {exc}")
        else:
            compiled = re.compile(re.escape(pattern), re.IGNORECASE)

        search_root = _sandbox_root
        if base:
            try:
                search_root = str(_resolve(base, check_sensitive=False))
            except SecurityError as exc:
                return self.error(str(exc))

        results: list[str] = []
        for dirpath, dirnames, filenames in os.walk(search_root):
            # Prune skipped directories
            dirnames[:] = [d for d in dirnames if d not in self._SKIP_DIRS]

            for fname in filenames:
                if include and not fnmatch.fnmatch(fname, include):
                    continue

                fpath = os.path.join(dirpath, fname)
                if is_binary_file(fpath):
                    continue

                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                        for lineno, line in enumerate(fh, 1):
                            if compiled.search(line):
                                try:
                                    rel = os.path.relpath(fpath, _sandbox_root)
                                except ValueError:
                                    rel = fpath
                                results.append(f"{rel}:{lineno}:{line.rstrip()}")
                                if len(results) >= self._MAX_RESULTS:
                                    break
                except OSError:
                    continue

                if len(results) >= self._MAX_RESULTS:
                    break
            if len(results) >= self._MAX_RESULTS:
                break

        if not results:
            return self.ok(f"No matches for '{pattern}'.")

        output = "\n".join(results)
        suffix = ""
        if len(results) >= self._MAX_RESULTS:
            suffix = f"\n\n... [results truncated at {self._MAX_RESULTS}]"
        return self.ok(f"Found {len(results)} match(es):\n{output}{suffix}")


# ===================================================================
# Self-register all tools
# ===================================================================

register_tool(BashTool())
register_tool(ReadFileTool())
register_tool(WriteFileTool())
register_tool(EditFileTool())
register_tool(GlobTool())
register_tool(GrepTool())
