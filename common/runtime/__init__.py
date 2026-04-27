"""Agentic runtime adapter package.

Provides a unified interface for calling LLM backends via ``adapter.py``.

Backends (set with AGENT_RUNTIME env var, default: "copilot-connect"):

  copilot-connect  — production backend; uses common/llm_client.py with
                     a three-tier fallback:
                       1. MOCK_LLM=1 → deterministic mock
                       2. Copilot CLI (requires COPILOT_GITHUB_TOKEN env var
                          AND the copilot binary in the container image)
                       3. OpenAI-compatible REST API (OPENAI_BASE_URL)

  mock             — always returns a fixed response; for unit tests only.

COPILOT_GITHUB_TOKEN:
  Only needed when you want the Copilot CLI code path (tier 2 above).
  For containers launched by Compass, this token is injected automatically
  via the ``passThroughEnv`` mechanism in registry-config.json — you do NOT
  need to set it in every agent's own .env file.  You only need to define
  it once in compass/.env (or as a host environment variable).

Usage::

    from common.runtime.adapter import get_runtime

    rt = get_runtime()  # reads AGENT_RUNTIME env var, defaults to copilot-connect
    result = rt.run(prompt="...", context={})
    # result: {summary, structured_output, artifacts, warnings, next_actions}
"""
