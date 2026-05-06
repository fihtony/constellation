# Skill: GitHub Copilot CLI Agent in Docker Container

## Summary

This skill describes how to install and use **GitHub Copilot CLI** inside a Docker container in non-interactive (programmatic) mode, authenticated via a fine-grained Personal Access Token.

Verified working: Copilot CLI v1.0.36, model `gpt-5-mini`, Node.js 22, Docker Desktop (macOS).

---

## Authentication

Copilot CLI checks credentials in this priority order:

1. `COPILOT_GITHUB_TOKEN` env var (highest priority — recommended for containers)
2. `GH_TOKEN` env var
3. `GITHUB_TOKEN` env var
4. OAuth token from system keychain
5. `gh auth token` fallback

**For containers, always use `COPILOT_GITHUB_TOKEN`.**

Constellation policy override:
- Even though Copilot CLI itself can fall back to `GH_TOKEN`, `GITHUB_TOKEN`, keychain, or `gh auth`, Constellation agents must not allow those fallback paths.
- Inside Constellation, only the file-backed `COPILOT_GITHUB_TOKEN` from `.env` is valid. Agent runtimes must build an isolated home/config directory and scrub all generic GitHub credential variables before launching `copilot`.
- If `copilot` cannot authenticate, cannot start, or exits non-zero, fail the task directly. Do not silently fall back to Copilot Connect or a mock backend.
- If a launcher or test injects a file-backed `COPILOT_GITHUB_TOKEN` into a child process, it must also set `CONSTELLATION_TRUSTED_ENV=1` for that child after removing inherited host GitHub credentials.

### Required PAT permissions

Create a fine-grained PAT at https://github.com/settings/personal-access-tokens/new:
- Under **Permissions → Account permissions** → select **Copilot Requests**

Supported token types: fine-grained PAT, OAuth token, GitHub App token  
Classic PAT (`ghp_`) is **NOT** supported.

---

## Dockerfile

```dockerfile
FROM node:22-slim

WORKDIR /workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub Copilot CLI globally
RUN npm install -g @github/copilot

# Your agent code
COPY . /workspace/

ENV PYTHONUNBUFFERED=1

CMD ["python3", "/workspace/your_agent.py"]
```

**Key points:**
- Base image: `node:22-slim` (Node.js 22+ is required)
- Install via npm: `npm install -g @github/copilot`
- No keychain or `libsecret` needed — token is passed via environment variable

---

## Non-Interactive Usage

Use `copilot -p` (prompt flag) and `-s` (silent/response-only flag):

```bash
# Simple question
copilot -sp "What is 2+2?"

# With explicit model selection
copilot --model gpt-5-mini -sp "Write a hello_world() function in Python"
```

**Flags:**
- `-p "PROMPT"` — pass prompt directly (non-interactive)
- `-s` — silent mode: output only Copilot's response, no extra usage info
- `--model MODEL` — select model (e.g. `gpt-5-mini`, `claude-sonnet-4.5`)

---

## Python Integration Pattern

```python
import os
import subprocess

from common.env_utils import build_isolated_copilot_env

COPILOT_TOKEN = os.environ.get("COPILOT_GITHUB_TOKEN", "")
MODEL = os.environ.get("COPILOT_MODEL", "gpt-5-mini")

def run_copilot(prompt: str, timeout: int = 60) -> tuple[bool, str]:
    """Run Copilot CLI non-interactively. Returns (success, response_text)."""
    if not COPILOT_TOKEN:
        return False, "COPILOT_GITHUB_TOKEN is not set."

    cmd = ["copilot", "--model", MODEL, "-sp", prompt]
    env = build_isolated_copilot_env(COPILOT_TOKEN, os.environ)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if result.returncode != 0:
            return False, f"Exit {result.returncode}: {result.stderr[:300]}"
        return True, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, f"Copilot CLI timed out after {timeout}s"
    except FileNotFoundError:
        return False, "copilot binary not found"
```

---

## Running the Test Container

```bash
docker build -f copilot-cli-test/Dockerfile -t constellation-copilot-cli-test:latest .

docker run --rm \
  -e COPILOT_GITHUB_TOKEN=<your-fine-grained-pat> \
  -e COPILOT_MODEL=gpt-5-mini \
  constellation-copilot-cli-test:latest \
  python3 /workspace/test_agent.py
```

Expected output:
```
[copilot-cli-test] Test 1: Basic question — what is 2+2?
[copilot-cli-test]   PASS — response: 4
[copilot-cli-test] Test 2: Code generation — write hello world in Python
[copilot-cli-test]   PASS — response preview: def hello_world(): ...
[copilot-cli-test] Test 3: Model selection — using model 'gpt-5-mini'
[copilot-cli-test]   PASS — response: READY
[copilot-cli-test] Passed: 3/3
[copilot-cli-test] All tests passed!
```

---

## Building an Agent on Copilot CLI

Instead of calling an LLM API directly, an agent can delegate all reasoning to Copilot CLI:

```python
from common.llm_client import generate_text  # standard LLM path

# OR: use Copilot CLI for richer agentic reasoning
ok, response = run_copilot(
    f"Analyze this task and return a JSON plan:\n{task_instruction}",
    timeout=120,
)
```

**When to use Copilot CLI vs direct LLM API:**

| Scenario | Use Copilot CLI | Use LLM API directly |
|---|---|---|
| Complex multi-step reasoning | ✅ | |
| File-reading context needed | ✅ (`@file` refs) | |
| Simple text generation | | ✅ (lower latency) |
| Structured JSON output | | ✅ (more reliable) |
| Cost-sensitive high-volume | | ✅ |

## Orchestration Pattern For Constellation Agents

When using Copilot CLI or Claude Code inside a Constellation agent, prefer this pattern:

1. Ask the runtime for a **structured JSON decision** such as pending tasks, next actions, or plan fields.
2. Keep the actual boundary operations in code: Registry capability lookup, A2A request construction, polling, retries, and artifact writing.
3. Treat runtime output as orchestration guidance, not as permission to bypass registered agents.

Example use case for Team Lead:

```python
result = get_runtime().run(
    prompt="Plan the next information-gathering actions and return JSON.",
    system_prompt="Use only the available capabilities listed in context.",
)
gather_plan = result["structured_output"]

for action in gather_plan.get("actions", []):
    if action.get("action") == "fetch_agent_context":
        _call_sync_agent(action["capability"], action["message"], ...)
```

This keeps Copilot CLI valuable for multi-step reasoning while preserving Constellation's audit, capability-discovery, and credential boundaries.

---

## Environment Variables

```env
# Required: Fine-grained PAT with "Copilot Requests" permission
COPILOT_GITHUB_TOKEN=<your-copilot-github-token>

# Optional: override model (default: gpt-5-mini)
COPILOT_MODEL=gpt-5-mini

# Optional: set custom Copilot home dir (default: ~/.copilot)
COPILOT_HOME=/workspace/.copilot
```

Do not set `GH_TOKEN` or `GITHUB_TOKEN` for Constellation agents. Those variables are intentionally ignored for runtime isolation.

---

## Known Limitations

- Copilot CLI is **interactive by default** — always use `-p` flag in containers
- The `--model` flag only works when authenticated; model names may change with CLI versions
- `/delegate` command requires GitHub authentication AND network access to GitHub.com
- Token expiry depends on PAT settings — rotate tokens regularly in production
- Copilot CLI does NOT support stdin piping; use `-p` flag only
