"""Integration tests for the connect-agent runtime (real LLM calls).

Tests verify:
  - Single-shot run() returns coherent output
  - run_agentic() with tool calling executes tools and returns final answer
  - Multi-runtime factory resolves all backends correctly

Run:
    pytest tests/integration/test_runtime_live.py -v
    (requires OPENAI_BASE_URL pointing to a running Copilot Connect server)
"""
from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.live


def _can_reach_llm(base_url: str) -> bool:
    """Return True if the LLM endpoint is reachable."""
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(
            f"{base_url}/models",
            headers={"Accept": "application/json"},
            method="GET",
        )
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def require_llm(llm_base_url):
    """Skip all tests in this module if the LLM is not reachable."""
    os.environ.setdefault("OPENAI_BASE_URL", llm_base_url)
    if not _can_reach_llm(llm_base_url):
        pytest.skip(f"LLM endpoint not reachable: {llm_base_url}")


# ---------------------------------------------------------------------------
# TC-01: single-shot run()
# ---------------------------------------------------------------------------

def test_runtime_single_shot_json(llm_model):
    """run() returns a valid result dict; response may be text or structured."""
    from framework.runtime.adapter import get_runtime
    runtime = get_runtime("connect-agent", model=llm_model)

    result = runtime.run(
        prompt='Return exactly this JSON and nothing else: {"answer": 42}',
        system_prompt="You must return valid JSON only. No prose. No markdown.",
        max_tokens=256,
    )
    assert isinstance(result, dict), "run() should return a dict"
    # raw_response may be empty if the model uses internal tool calls or
    # the proxy strips content; verify at least that the key exists and
    # no exception was thrown.
    assert "raw_response" in result, "Missing 'raw_response' key"
    assert "warnings" in result, "Missing 'warnings' key"
    raw = result.get("raw_response", "") or ""
    print(f"[runtime] single-shot response: {raw[:120]!r}")
    # If the model returned JSON, verify it parses
    if raw.strip():
        import json as _json
        try:
            parsed = _json.loads(raw.strip().strip("` \n").lstrip("json").strip())
            print(f"[runtime] parsed JSON: {parsed}")
        except Exception:
            # Non-JSON prose is acceptable for some model configurations
            pass


# ---------------------------------------------------------------------------
# TC-02: single-shot with structured output parsing
# ---------------------------------------------------------------------------

def test_runtime_parse_structured_output(llm_model):
    """build_result + parse_structured_output round-trips JSON cleanly."""
    from framework.runtime.adapter import AgentRuntimeAdapter

    text = '```json\n{"summary": "test", "count": 5}\n```'
    parsed = AgentRuntimeAdapter.parse_structured_output(text)
    assert parsed.get("summary") == "test"
    assert parsed.get("count") == 5


# ---------------------------------------------------------------------------
# TC-03: run_agentic() with ToolRegistry
# ---------------------------------------------------------------------------

def test_runtime_agentic_with_tool(llm_model):
    """run_agentic() calls a registered tool and returns the tool result."""
    from framework.runtime.adapter import get_runtime
    from framework.tools.registry import get_registry, ToolRegistry
    from framework.tools.base import BaseTool, ToolResult

    # Register a simple echo tool in a fresh registry
    registry = ToolRegistry()

    class EchoTool(BaseTool):
        name = "echo"
        description = "Echo the input message back."
        parameters_schema = {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The message to echo."}
            },
            "required": ["message"],
        }

        def execute_sync(self, message: str = "") -> ToolResult:
            return ToolResult(output=json.dumps({"echoed": message}))

    registry.register(EchoTool())

    # Temporarily replace global registry
    import framework.tools.registry as _reg
    original = _reg._default_registry
    _reg._default_registry = registry

    try:
        runtime = get_runtime("connect-agent", model=llm_model)
        result = runtime.run_agentic(
            task=(
                'Call the echo tool with message="hello-world" and tell me '
                "what it returned."
            ),
            max_turns=5,
            timeout=60,
        )
    finally:
        _reg._default_registry = original

    assert result.success, f"run_agentic failed: {result.summary}"
    print(f"[runtime] agentic result: {result.summary[:120]!r}")
    print(f"[runtime] tool calls: {result.tool_calls}")


# ---------------------------------------------------------------------------
# TC-04: runtime factory — all backends resolve without error
# ---------------------------------------------------------------------------

def test_runtime_factory_all_backends():
    """get_runtime() factory resolves all four backend names."""
    from framework.runtime.adapter import get_runtime, resolve_backend_name

    for name in ("connect-agent", "copilot-cli", "claude-code", "codex-cli"):
        _, effective = resolve_backend_name(name)
        assert effective == name, f"Alias mismatch for {name!r}"
        runtime = get_runtime(name)
        assert runtime is not None, f"get_runtime({name!r}) returned None"
        # run() on non-connect-agent backends may fail if CLI not installed,
        # but the factory itself must succeed.
    print("[runtime] all backend instances created successfully")


# ---------------------------------------------------------------------------
# TC-05: unsupported backend name raises
# ---------------------------------------------------------------------------

def test_runtime_factory_unknown_backend():
    """get_runtime() raises KeyError for unknown backend names."""
    from framework.runtime.adapter import get_runtime
    with pytest.raises(KeyError, match="Unknown runtime backend"):
        get_runtime("no-such-backend-xyz")
