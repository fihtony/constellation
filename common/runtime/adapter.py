"""Agentic runtime adapter — unified interface for LLM/CLI backends.

All agents that need agentic reasoning MUST use this adapter instead of
calling llm_client.py directly.  The backend is selected via the
AGENT_RUNTIME environment variable.

Supported backends
------------------
copilot-connect (default)
    The production backend.  Delegates to ``common/llm_client.py`` which
    itself has a three-tier fallback chain:

      1. ``MOCK_LLM=1`` env var → deterministic mock (fastest, no network)
      2. GitHub Copilot CLI binary (when ``COPILOT_GITHUB_TOKEN`` is set
         *and* the ``copilot`` binary is present in the container image)
      3. OpenAI-compatible REST API at ``OPENAI_BASE_URL``

    The name "copilot-connect" refers to the *agent runtime layer*, not
    the Copilot CLI specifically.  Even when the CLI is unavailable, the
    backend falls through to the OpenAI API automatically.

mock
    Returns deterministic fixed responses.  Use in unit tests only.

Model priority (highest to lowest)
-----------------------------------
1. ``model`` parameter on ``adapter.run(...)``
2. ``AGENT_MODEL`` environment variable
3. Backend default (``OPENAI_MODEL`` / ``gpt-5-mini``)

Output contract
---------------
Every backend returns a dict with these keys::

    {
        "summary":           str,   # human-readable summary of what was done
        "structured_output": dict,  # parsed JSON output from the model
        "artifacts":         list,  # file artifacts produced [{path, type}]
        "warnings":          list,  # non-fatal issues encountered
        "next_actions":      list,  # suggested follow-up actions
        "raw_response":      str,   # raw text from the model (debug)
    }
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class AgentRuntimeAdapter(ABC):
    """Abstract base class for all runtime backends."""

    @abstractmethod
    def run(
        self,
        prompt: str,
        context: dict | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        max_tokens: int = 4096,
    ) -> dict:
        """Execute a prompt and return a structured result.

        Parameters
        ----------
        prompt:
            The user-facing instruction / task description.
        context:
            Optional dict with additional context (injected into system prompt).
        system_prompt:
            Explicit system prompt.  When omitted, a sensible default is used.
        model:
            Override the default model for this call only.
        timeout:
            Maximum seconds to wait for a response.
        max_tokens:
            Maximum tokens in the response.

        Returns
        -------
        dict
            ``{summary, structured_output, artifacts, warnings, next_actions, raw_response}``
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_structured_output(text: str) -> dict:
        """Try to extract a JSON object from the model response."""
        text = (text or "").strip()
        # Strip markdown fences
        if text.startswith("```"):
            lines = text.splitlines()
            start = 1
            end = len(lines)
            while end > start and lines[end - 1].strip() in ("```", ""):
                end -= 1
            text = "\n".join(lines[start:end]).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        return {}

    @staticmethod
    def _build_result(raw: str, structured: dict | None = None) -> dict:
        """Build the standard result dict."""
        if structured is None:
            structured = AgentRuntimeAdapter._parse_structured_output(raw)
        return {
            "summary": structured.get("summary") or raw[:500],
            "structured_output": structured,
            "artifacts": structured.get("artifacts") or [],
            "warnings": structured.get("warnings") or [],
            "next_actions": structured.get("next_actions") or [],
            "raw_response": raw,
        }


# ---------------------------------------------------------------------------
# CopilotConnect backend (default)
# ---------------------------------------------------------------------------

class CopilotConnectAdapter(AgentRuntimeAdapter):
    """Calls an OpenAI-compatible API endpoint via ``common/llm_client.py``.

    This is the default production backend when running with Copilot Connect
    (localhost:1288/v1) or any compatible proxy.  It is also the fallback when
    the Copilot CLI is not available.
    """

    DEFAULT_SYSTEM = (
        "You are an expert software engineering agent. "
        "When asked to produce structured data, always respond with valid JSON. "
        "Be concise and precise."
    )

    def run(
        self,
        prompt: str,
        context: dict | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        max_tokens: int = 4096,
    ) -> dict:
        from common.llm_client import generate_text  # late import to avoid circular

        effective_model = (
            model
            or os.environ.get("AGENT_MODEL")
            or os.environ.get("OPENAI_MODEL")
            or "gpt-5-mini"
        )
        effective_system = system_prompt or self.DEFAULT_SYSTEM

        if context:
            ctx_str = json.dumps(context, ensure_ascii=False, indent=2)
            effective_system = f"{effective_system}\n\nContext:\n{ctx_str}"

        try:
            raw = generate_text(
                prompt,
                actor="[runtime:copilot-connect]",
                system_prompt=effective_system,
                model=effective_model,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            return self._build_result(
                "",
                {
                    "summary": f"LLM call failed: {exc}",
                    "warnings": [str(exc)],
                    "structured_output": {},
                    "artifacts": [],
                    "next_actions": [],
                },
            )

        return self._build_result(raw)


# ---------------------------------------------------------------------------
# Mock backend (tests only)
# ---------------------------------------------------------------------------

class MockAdapter(AgentRuntimeAdapter):
    """Returns a deterministic mock response for unit tests.

    Set MOCK_RUNTIME_RESPONSE env var to override the default response JSON.
    """

    DEFAULT_RESPONSE = json.dumps({
        "summary": "Mock response: task acknowledged.",
        "structured_output": {},
        "artifacts": [],
        "warnings": [],
        "next_actions": [],
    })

    def run(
        self,
        prompt: str,
        context: dict | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        max_tokens: int = 4096,
    ) -> dict:
        raw = os.environ.get("MOCK_RUNTIME_RESPONSE", self.DEFAULT_RESPONSE)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"summary": raw}
        return self._build_result(raw, data)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_BACKENDS: dict[str, type[AgentRuntimeAdapter]] = {
    "copilot-connect": CopilotConnectAdapter,
    "mock": MockAdapter,
}

# Cached instances per backend name
_INSTANCES: dict[str, AgentRuntimeAdapter] = {}


def get_runtime(
    backend: str | None = None,
    model: str | None = None,
) -> AgentRuntimeAdapter:
    """Return a runtime adapter instance.

    Parameters
    ----------
    backend:
        Backend name.  If omitted, reads ``AGENT_RUNTIME`` env var, then falls
        back to ``"copilot-connect"``.
    model:
        Default model override.  Sets ``AGENT_MODEL`` env var for this process.

    Returns
    -------
    AgentRuntimeAdapter
    """
    effective_backend = (
        backend
        or os.environ.get("AGENT_RUNTIME", "copilot-connect")
    ).lower()

    if model:
        os.environ["AGENT_MODEL"] = model

    if effective_backend not in _BACKENDS:
        print(
            f"[runtime] Unknown backend '{effective_backend}', "
            f"falling back to 'copilot-connect'."
        )
        effective_backend = "copilot-connect"

    if effective_backend not in _INSTANCES:
        _INSTANCES[effective_backend] = _BACKENDS[effective_backend]()

    return _INSTANCES[effective_backend]
