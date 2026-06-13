"""Claude Code CLI runtime adapter.

Invokes the ``claude`` subprocess (Anthropic Claude Code) for all LLM work.
When framework tools are registered, they are exposed to the CLI via a
lightweight MCP stdio bridge so Claude can call them from within its own
tool-calling loop — no other runtime is ever used as a fallback.

Backend name: ``claude-code``
Model: AGENT_MODEL env var → DEFAULT_MODEL.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from shutil import which

from framework.runtime.adapter import DEFAULT_MODEL, AgenticResult, AgentRuntimeAdapter

_SINGLE_SHOT_SYSTEM = (
    "You are an expert AI agent operating inside the Constellation system. "
    "Return valid JSON when structured output is requested. Be concise."
)

# ---------------------------------------------------------------------------
# MCP bridge script — written to a temp file, run by claude as a subprocess.
# Reads CONSTELLATION_TOOLS_URL from env and proxies JSON-RPC tool calls to
# the local HTTP ToolsHTTPServer running in the parent process.
# ---------------------------------------------------------------------------
# Credential env vars that must NEVER be passed to the claude subprocess.
# Claude accesses external services only through the MCP tool bridge (which
# calls Python tools in the parent process where credentials live). Passing
# these directly would allow claude to use raw git/gh commands with real creds.
_CLAUDE_ENV_STRIP = frozenset({
    "GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN",
    "SCM_TOKEN", "SCM_USERNAME", "SCM_PASSWORD",
    "JIRA_TOKEN", "JIRA_EMAIL",
    "FIGMA_TOKEN",
    "STITCH_API_KEY",
    "OPENAI_API_KEY",
    "GCM_CREDENTIAL_STORE", "CREDENTIAL_HELPER",
    "GCM_INTERACTIVE",
    # Test-prefixed variants
    "TEST_JIRA_TOKEN", "TEST_SCM_TOKEN", "TEST_FIGMA_TOKEN", "TEST_STITCH_API_KEY",
    "TEST_GITHUB_TOKEN",
})


def _build_claude_env(extra: dict | None = None) -> dict:
    """Build subprocess env for the claude CLI.

    Strips all service credential vars so claude cannot use raw git/gh
    commands to reach external services. Claude's own Anthropic auth
    (~/.claude/) is preserved because HOME is unchanged.

    Also sets git/gh isolation flags to prevent credential fallback:
      - GIT_TERMINAL_PROMPT=0: no interactive password prompt
      - GIT_CONFIG_NOSYSTEM=1: ignore /etc/gitconfig
      - GH_CONFIG_DIR: isolated empty dir (blocks 'gh auth' fallback)
    """
    env = {k: v for k, v in os.environ.items() if k not in _CLAUDE_ENV_STRIP}

    # Create an isolated temporary git config that disables credential helpers.
    # This is written lazily — reuse it across calls within the same process.
    global _isolated_gitconfig_path
    if _isolated_gitconfig_path is None or not os.path.exists(_isolated_gitconfig_path):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".gitconfig-isolated", delete=False, prefix="cc_gitcfg_"
        )
        tmp.write(
            "[credential]\n\thelper =\n"
            "[safe]\n\tdirectory = *\n"
            "[user]\n\tname = constellation-agent\n"
            "\temail = constellation@noreply.local\n"
        )
        tmp.close()
        _isolated_gitconfig_path = tmp.name

    # Isolated GH_CONFIG_DIR: empty dir so gh CLI cannot use host 'gh auth' creds
    if _isolated_gh_config_dir is None:
        _init_isolated_gh_config()

    env.update({
        "GIT_CONFIG_GLOBAL": _isolated_gitconfig_path,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GCM_INTERACTIVE": "never",
        "GH_CONFIG_DIR": _isolated_gh_config_dir or "",
    })
    if extra:
        env.update(extra)
    return env


# Module-level cache for isolated git/gh config paths (created once per process)
_isolated_gitconfig_path: str | None = None
_isolated_gh_config_dir: str | None = None


def _init_isolated_gh_config() -> None:
    global _isolated_gh_config_dir
    gh_dir = os.path.join(tempfile.gettempdir(), "constellation-claude-gh-isolated")
    os.makedirs(gh_dir, exist_ok=True)
    _isolated_gh_config_dir = gh_dir


_MCP_BRIDGE_SCRIPT = textwrap.dedent("""\
    #!/usr/bin/env python3
    \"\"\"Constellation MCP stdio bridge.

    Mode A (default): proxy tool calls to local parent HTTP bridge via CONSTELLATION_TOOLS_URL.
    Mode B (no-socket fallback): call ToolRegistry directly in this process.
    \"\"\"
    import json, os, sys, urllib.request, importlib

    TOOLS_URL = os.environ.get("CONSTELLATION_TOOLS_URL", "")
    TOOL_NAMES = json.loads(os.environ.get("CONSTELLATION_TOOL_NAMES_JSON", "[]"))
    BOOTSTRAP = json.loads(os.environ.get("CONSTELLATION_TOOL_BOOTSTRAP_JSON", "[]"))

    _REGISTRY = None

    def _init_registry_mode():
        global _REGISTRY
        from framework.tools.registry import get_registry
        _REGISTRY = get_registry()
        for entry in BOOTSTRAP:
            if ":" not in entry:
                continue
            mod_name, fn_name = entry.split(":", 1)
            try:
                mod = importlib.import_module(mod_name)
                fn = getattr(mod, fn_name)
                fn()
            except Exception:
                pass


    def _get(path):
        with urllib.request.urlopen(f"{TOOLS_URL}{path}", timeout=120) as r:
            return json.loads(r.read())


    def _post(path, data):
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            f"{TOOLS_URL}{path}", data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())


    def _handle(req):
        method = req.get("method", "")
        id_ = req.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": id_, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "constellation", "version": "1.0"},
            }}
        if method.startswith("notifications/"):
            return None  # notifications need no response
        if method == "tools/list":
            if TOOLS_URL:
                tools = _get("/tools")
            else:
                schemas = _REGISTRY.list_schemas(TOOL_NAMES or None)
                tools = [{
                    "name": s["function"]["name"],
                    "description": s["function"].get("description", ""),
                    "inputSchema": s["function"].get("parameters", {"type": "object", "properties": {}}),
                } for s in schemas]
            return {"jsonrpc": "2.0", "id": id_, "result": {"tools": tools}}
        if method == "tools/call":
            p = req.get("params", {})
            if TOOLS_URL:
                result = _post("/tools/call",
                               {"name": p.get("name"), "arguments": p.get("arguments", {})})
            else:
                out = _REGISTRY.execute_sync(p.get("name", ""), p.get("arguments", {}))
                result = {"content": [{"type": "text", "text": out}]}
            return {"jsonrpc": "2.0", "id": id_, "result": result}
        return {"jsonrpc": "2.0", "id": id_,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}


    if not TOOLS_URL:
        _init_registry_mode()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()
""")


def _find_claude_cli() -> str | None:
    for cmd in ("claude", "claude-code"):
        if which(cmd):
            return cmd
    return None


def _claude_mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Translate an MCP server/tool pair to Claude Code's tool id format."""
    safe_server = str(server_name).strip().replace("-", "_")
    safe_tool = str(tool_name).strip()
    return f"mcp__{safe_server}__{safe_tool}"


# Map Anthropic API model IDs → Claude Code CLI model aliases.
# The CLI accepts short aliases (haiku, sonnet, opus) but not full API IDs.
_MODEL_ALIAS: dict[str, str] = {
    # Haiku 4.5
    "claude-haiku-4-5-20251001": "haiku",
    "claude-haiku-4-5": "haiku",
    # Sonnet 4.6
    "claude-sonnet-4-6": "sonnet",
    # Opus 4.6
    "claude-opus-4-6": "opus",
    # Opus 4.7
    "claude-opus-4-7": "opus",
}


def _cli_model(model: str | None) -> str:
    """Resolve the model string for the claude CLI.

    API model IDs (e.g. claude-haiku-4-5-20251001) are mapped to the short
    CLI aliases the claude binary accepts (e.g. haiku).  Short aliases and
    unrecognised names are passed through unchanged.
    """
    raw = (model or os.environ.get("AGENT_MODEL", DEFAULT_MODEL)).strip()
    return _MODEL_ALIAS.get(raw, raw)


def _effective_model(model: str | None) -> str:
    return _cli_model(model)


# ---------------------------------------------------------------------------
# Tool bootstrap inference for no-socket bridge mode
# ---------------------------------------------------------------------------

def _infer_tool_bootstrap_entries(tool_names: list[str] | None) -> list[str]:
    names = set(tool_names or [])
    entries: list[str] = []
    if names & {
        "read_pdf", "read_docx", "read_txt", "read_csv", "read_xlsx", "read_xls",
        "read_pptx", "list_directory", "write_workspace", "write_file",
        "organize_folder", "organize_move_file",
    }:
        entries.append("agents.office.office_tools:register_office_tools")
    if names & {"dispatch_development_task", "dispatch_office_task"}:
        entries.append("agents.compass.tools:register_compass_tools")
    if names & {
        "read_file", "write_file", "edit_file", "search_code", "run_command",
        "scm_clone", "scm_branch", "scm_commit", "scm_push", "scm_create_pr",
    }:
        entries.append("agents.web_dev.tools:register_web_dev_tools")
        entries.append("agents.web_dev.coding_tools:register_web_dev_coding_tools")
    # Stable dedupe preserving order
    return list(dict.fromkeys(entries))


# ---------------------------------------------------------------------------
# In-process HTTP server exposing ToolRegistry to the MCP bridge subprocess
# ---------------------------------------------------------------------------

class _ToolsHTTPServer:
    """Serves GET /tools and POST /tools/call backed by a ToolRegistry."""

    def __init__(self, registry, tool_names: list[str] | None) -> None:
        self._registry = registry
        self._tool_names = tool_names
        self._server: HTTPServer | None = None

    def start(self) -> int:
        import random
        for _ in range(20):
            port = random.randint(49152, 65535)
            try:
                self._server = HTTPServer(("127.0.0.1", port), self._make_handler())
                break
            except OSError:
                continue
        else:
            raise RuntimeError("Could not bind a free port for ToolsHTTPServer")
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return port

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()

    def _make_handler(self):
        registry = self._registry
        tool_names = self._tool_names

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):  # suppress access logs
                pass

            def _send_json(self, data, status: int = 200) -> None:
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path != "/tools":
                    self.send_error(404)
                    return
                schemas = registry.list_schemas(tool_names)
                tools = [
                    {
                        "name": s["function"]["name"],
                        "description": s["function"].get("description", ""),
                        "inputSchema": s["function"].get(
                            "parameters", {"type": "object", "properties": {}}
                        ),
                    }
                    for s in schemas
                ]
                self._send_json(tools)

            def do_POST(self):
                if self.path != "/tools/call":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length))
                result = registry.execute_sync(
                    payload.get("name", ""),
                    payload.get("arguments", {}),
                )
                self._send_json({"content": [{"type": "text", "text": result}]})

        return _Handler


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ClaudeCodeAdapter(AgentRuntimeAdapter):
    """Runtime adapter that delegates all LLM calls to the ``claude`` CLI.

    Single-shot (``run``) and agentic without tools (``run_agentic``) both
    use ``claude --print`` via subprocess.

    Agentic with framework tools additionally starts an in-process MCP bridge
    so the ``claude`` CLI can call Python-registered tools from within its own
    tool-calling loop.  No other runtime is ever invoked as a fallback.
    """

    def run(
        self,
        prompt: str,
        context: dict | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        max_tokens: int = 4096,
        plugin_manager=None,
        cwd: str | None = None,
        disallowed_tools: list[str] | None = None,
    ) -> dict:
        cli = _find_claude_cli()
        if not cli:
            raise RuntimeError(
                "claude-code: 'claude' CLI not found in PATH. "
                "Install Claude Code CLI and authenticate with 'claude' before running."
            )

        full_prompt = self.build_prompt(
            prompt,
            system_prompt=system_prompt or _SINGLE_SHOT_SYSTEM,
            context=context,
        )
        model_id = _effective_model(model)
        cmd = [cli, "--print", "--dangerously-skip-permissions", "--model", model_id]
        # ``runtime.run`` is supposed to be a single-shot text call.
        # Without ``--tools ""`` the local ``claude`` CLI gives the LLM
        # access to its full native tool surface (Read/Write/Edit/Bash
        # /WebSearch/...) and, when ``cwd`` points at a writable
        # directory, the LLM can drop stray files there even when the
        # system prompt told it not to.  Callers that have already
        # extracted the input and only need text back opt into the
        # tool-free mode by passing ``disallowed_tools``; we honour it
        # the same way ``run_agentic`` does for the MCP-only path
        # (see ``--tools ""`` at the bottom of this module) so the
        # single-shot and multi-shot code paths share one escape hatch.
        if disallowed_tools:
            cmd += ["--tools", ""]

        try:
            proc = subprocess.run(
                cmd,
                input=full_prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=_build_claude_env(),
                cwd=cwd,
            )
            if proc.returncode != 0:
                return self.build_failure_result(
                    f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}",
                    backend_used="claude-code",
                )
            return self.build_result(proc.stdout.strip(), backend_used="claude-code")
        except subprocess.TimeoutExpired:
            return self.build_failure_result(
                f"claude timed out after {timeout}s", backend_used="claude-code"
            )
        except Exception as exc:
            return self.build_failure_result(
                f"claude error: {exc}", backend_used="claude-code"
            )

    def run_agentic(
        self,
        task: str,
        *,
        system_prompt: str | None = None,
        cwd: str | None = None,
        tools: list[str] | None = None,
        mcp_servers: dict | None = None,
        allowed_tools: list[str] | None = None,
        max_turns: int = 50,
        timeout: int = 1800,
        on_progress=None,
        continuation: str | None = None,
        plugin_manager=None,
    ) -> AgenticResult:
        """Multi-turn agentic execution via the claude CLI.

        When *tools* are provided, the registered Python tools are exposed to
        the CLI through an auto-generated MCP bridge server running in a
        background thread.  The caller-supplied *mcp_servers* (e.g. Jira, SCM)
        are merged into the same MCP config file.
        """
        cli = _find_claude_cli()
        if not cli:
            raise RuntimeError(
                "claude-code: 'claude' CLI not found in PATH. "
                "Install Claude Code CLI and authenticate with 'claude' before running."
            )

        model_id = _effective_model(None)
        full_prompt = task
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{task}"

        # --- Build MCP config -------------------------------------------
        merged_mcp: dict = dict(mcp_servers or {})
        http_srv: _ToolsHTTPServer | None = None
        bridge_path: str | None = None
        mcp_config_path: str | None = None
        no_socket_bridge_mode = False
        bridge_env: dict[str, str] = {}

        if tools:
            from framework.tools.registry import get_registry
            registry = get_registry()
            try:
                http_srv = _ToolsHTTPServer(registry, tools)
                port = http_srv.start()
                bridge_env["CONSTELLATION_TOOLS_URL"] = f"http://127.0.0.1:{port}"
            except Exception:
                # Some sandboxed runtimes disallow local socket bind.
                # Fall back to direct registry mode inside the bridge process.
                no_socket_bridge_mode = True
                http_srv = None
                bridge_env["CONSTELLATION_TOOL_NAMES_JSON"] = json.dumps(tools)
                bridge_env["CONSTELLATION_TOOL_BOOTSTRAP_JSON"] = json.dumps(
                    _infer_tool_bootstrap_entries(tools)
                )

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, prefix="cc_mcp_bridge_"
            ) as tmp:
                tmp.write(_MCP_BRIDGE_SCRIPT)
                bridge_path = tmp.name

            merged_mcp["constellation-tools"] = {
                "command": "python3",
                "args": [bridge_path],
                "env": bridge_env,
            }

        cmd = [cli, "--print", "--dangerously-skip-permissions", "--model", model_id]

        if merged_mcp:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, prefix="cc_mcp_cfg_"
            ) as tmp:
                json.dump({"mcpServers": merged_mcp}, tmp)
                mcp_config_path = tmp.name
            cmd += ["--mcp-config", mcp_config_path, "--strict-mcp-config"]

        effective_allowed_tools = list(allowed_tools or [])
        if not effective_allowed_tools and tools:
            effective_allowed_tools = [
                _claude_mcp_tool_name("constellation-tools", tool_name)
                for tool_name in tools
            ]

        if effective_allowed_tools:
            cmd += ["--allowedTools", ",".join(effective_allowed_tools)]
            if all(str(name).startswith("mcp__") for name in effective_allowed_tools):
                # Disable Claude's built-in filesystem/shell tools when this run
                # should operate exclusively through Constellation MCP tools.
                cmd += ["--tools", ""]

        # --- Run claude -------------------------------------------------
        # Build credential-stripped env; inject MCP bridge URL when bridge is active
        _extra_env: dict = {}
        if no_socket_bridge_mode:
            # Ensure bridge subprocess can import repository modules when no-socket mode
            # needs to bootstrap and register tools directly.
            cwd_for_path = cwd or os.getcwd()
            existing = os.environ.get("PYTHONPATH", "")
            _extra_env["PYTHONPATH"] = (
                f"{cwd_for_path}{os.pathsep}{existing}" if existing else cwd_for_path
            )
        try:
            proc = subprocess.run(
                cmd,
                input=full_prompt,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=timeout,
                env=_build_claude_env(_extra_env or None),
            )
            stdout = proc.stdout.strip()
            stderr = proc.stderr.strip()

            if proc.returncode != 0:
                return AgenticResult(
                    success=False,
                    summary=f"claude exited {proc.returncode}: {stderr[:500]}",
                    backend_used="claude-code",
                )

            if on_progress:
                on_progress("claude-code completed")

            return AgenticResult(
                success=True,
                summary=stdout or "Done.",
                raw_output=stdout,
                backend_used="claude-code",
                turns_used=1,
            )
        except subprocess.TimeoutExpired:
            return AgenticResult(
                success=False,
                summary=f"claude timed out after {timeout}s",
                backend_used="claude-code",
            )
        except Exception as exc:
            return AgenticResult(
                success=False,
                summary=f"claude error: {exc}",
                backend_used="claude-code",
            )
        finally:
            if http_srv:
                http_srv.stop()
            for path in filter(None, [bridge_path, mcp_config_path]):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def supports_mcp(self) -> bool:
        return True
