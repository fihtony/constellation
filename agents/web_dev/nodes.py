"""Web Dev Agent workflow nodes.

Each node receives the full workflow state dict and returns a partial dict
that is merged back into state by the WorkflowRunner.

Design pattern — "Graph outside, ReAct inside":
- Macro lifecycle (node order, branching, looping) is the graph's job.
- Open-ended implementation work is delegated to runtime.run_agentic().
- Bounded single-shot decisions (branch name, PR title) use runtime.run().
- Nodes degrade gracefully when no runtime is available (unit-test path).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path as _Path
from typing import Any

from framework.config import load_agent_config as _load_agent_cfg
from framework.context_budget import (
    compact_delivery_plan,
    compact_jira_context,
    text_for_prompt,
)
from framework.devlog import AgentLogger
from framework.major_step import (
    LIFECYCLE_DONE,
    LIFECYCLE_FAILED,
    LIFECYCLE_RUNNING,
    LIFECYCLE_WAITING_FOR_USER,
    LIFECYCLE_WARNING,
    record_major_step,
)
from framework.review_contract import REVIEW_ISSUE_SCHEMA, issue_blocks_merge

# Load agent_id from config.yaml — single source of truth for identity
_AGENT_ID: str = _load_agent_cfg(
    _Path(__file__).parent.name.replace("_", "-")
).get("agent_id", _Path(__file__).parent.name.replace("_", "-"))


def _logger(state: dict) -> AgentLogger:
    """Return an AgentLogger for this agent using the task_id stored in state."""
    return AgentLogger(state.get("_task_id", ""), _AGENT_ID)


def _record_timeline_step(
    state: dict,
    *,
    step_key: str,
    title: str,
    lifecycle_state: str = LIFECYCLE_RUNNING,
    summary_template: str = "",
    summary_facts: dict | None = None,
    round: int = 0,
    conditional: bool = False,
) -> None:
    task_id = state.get("_compass_task_id") or state.get("_task_id") or state.get("task_id") or ""
    if not task_id:
        return
    try:
        record_major_step(
            task_id,
            step_key=step_key,
            title=title,
            agent="web-dev",
            lifecycle_state=lifecycle_state,
            summary_template=summary_template,
            summary_facts=summary_facts,
            round=round,
            conditional=conditional,
            orchestrator_task_id=state.get("_compass_task_id") or task_id,
            progress_sink=state.get("_major_step_progress_sink"),
            task_store=state.get("_task_store"),
        )
    except Exception as exc:  # noqa: BLE001
        _logger(state).debug("major-step write skipped", step_key=step_key, error=str(exc))


def _boundary_log(state: dict, agent_id: str, message: str, **kwargs: Any) -> None:
    """Deprecated proxy log — kept only to avoid breaking call sites in this file.

    web_dev/nodes.py should pass task_id to boundary tool args instead.
    This function is a no-op: each boundary agent logs to its own directory.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _summarize_jira_context(jira_ctx: dict, max_chars: int = 3000) -> str:
    """Extract only essential Jira fields and truncate to avoid prompt overflow."""
    return compact_jira_context(jira_ctx, max_chars=max_chars)


def _safe_json(text: str, fallback: Any = None) -> Any:
    """Extract and parse the first JSON object/array from *text*.

    Thin wrapper around :func:`framework.json_extract.extract_first_json` so
    every agent uses the same balanced-brace, ``<think>``-aware, fence-aware
    parser. Returns *fallback* when no JSON can be extracted.
    """
    from framework.json_extract import extract_first_json

    if not text:
        return fallback
    result = extract_first_json(text)
    return fallback if result is None else result


def _run_mandatory_validation(repo_path: str, workspace_path: str, cycle: int) -> dict:
    """Run install, build, and tests through the deterministic validation script."""
    script_path = _Path(__file__).resolve().parent / "scripts" / "validate_project.py"
    output_path = ""
    if workspace_path:
        output_path = os.path.join(
            workspace_path,
            _AGENT_ID,
            "test-results",
            f"validation-run-{cycle}.json",
        )

    command = [sys.executable, str(script_path), repo_path]
    if output_path:
        command.extend(["--output", output_path])

    proc = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=int(os.environ.get("WEB_DEV_VALIDATION_TIMEOUT", "2400")),
        check=False,
    )
    data = _safe_json(proc.stdout or "", fallback={}) or {}
    if output_path and os.path.isfile(output_path):
        try:
            with open(output_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
    if proc.returncode != 0:
        data.setdefault("failed", 1)
        data.setdefault("errors", []).append("mandatory validation script failed")
    data.setdefault("output", proc.stdout or "")
    return data


def _tail_text(text: str, limit: int = 600) -> str:
    """Return the last *limit* characters of text for compact logging."""
    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[-limit:]


def _runtime_backend_label(runtime: Any, result: Any) -> str:
    backend = str(getattr(result, "backend_used", "") or "").strip()
    if backend:
        return backend
    backend = str(getattr(runtime, "backend", "") or "").strip()
    if backend:
        return backend
    return runtime.__class__.__name__ if runtime is not None else "runtime"


def _agentic_failure_allows_partial_progress(summary: str) -> bool:
    """Return True when a backend failure can still flow into validation.

    Text-managed CLI backends sometimes finish useful file edits but fail to
    emit the final managed-loop JSON envelope. That is a protocol/finalization
    failure, not evidence that the code is unusable. The deterministic build,
    test, self-check, screenshot, and PR gates are better judges once files
    exist. Permission failures and tool-policy violations still fail closed.
    """
    lowered = str(summary or "").lower()
    recoverable_markers = (
        "did not return valid managed-loop json",
        "timed out",
        "timeout",
        "max turns",
        "after 50 turns",
        "after 20 turns",
        "after 15 turns",
    )
    blocking_markers = (
        "unauthorized tool",
        "permission denied",
        "does not support",
        "rejected",
    )
    return any(marker in lowered for marker in recoverable_markers) and not any(
        marker in lowered for marker in blocking_markers
    )


def _state_env_int(
    state: dict,
    state_key: str,
    metadata_key: str,
    env_name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int = 300,
) -> int:
    """Read an integer budget from state metadata, env, then default."""
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    raw = state.get(state_key)
    if raw in (None, "") and metadata_key:
        raw = metadata.get(metadata_key)
    if raw in (None, ""):
        raw = os.environ.get(env_name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, min(value, maximum))


def _repo_changed_file_set(repo_path: str, state: dict | None = None) -> set[str]:
    """Return branch/worktree changed files as a set."""
    if state is not None:
        return set(_workflow_changed_files(state))
    return set(_git_branch_changed_files(repo_path)) | set(_git_worktree_changed_files(repo_path))


def _agentic_result_has_mutating_tool_call(result: Any) -> bool:
    """Return True when a managed/native result reports file-mutating tools."""
    for call in getattr(result, "tool_calls", []) or []:
        if not isinstance(call, dict):
            continue
        tool_name = str(call.get("tool") or call.get("name") or "").strip()
        if tool_name in {"write_file", "edit_file"}:
            return True
    return False


def _agentic_partial_progress_detected(
    result: Any,
    *,
    changed_before: set[str],
    changed_after: set[str],
) -> bool:
    """Return True when a recoverable backend stop still produced edits.

    This keeps behavior consistent across managed text backends that sometimes
    perform the requested edits but fail to emit the final JSON envelope.
    Deterministic build/test/self-assessment gates still decide correctness.
    """
    if getattr(result, "success", False):
        return False
    if not _agentic_failure_allows_partial_progress(getattr(result, "summary", "")):
        return False
    if changed_after != changed_before:
        return True
    return _agentic_result_has_mutating_tool_call(result)


def _agentic_repair_summary(result: Any, step_name: str) -> str:
    backend = str(getattr(result, "backend_used", "") or "agentic backend")
    summary = str(getattr(result, "summary", "") or "")
    return (
        f"Partial {step_name} progress ({backend} stopped before final JSON). "
        "Repository changes/tool edits were observed; deterministic validation "
        f"will decide. Error: {summary[:200]}"
    )


def _agentic_policy_for_state(state: dict, runtime: Any):
    from framework.agentic_policy import (
        agentic_policy_kwargs,
        build_agentic_execution_policy,
    )

    allowed_tools = state.get("_allowed_tools") or []
    policy = build_agentic_execution_policy(runtime, allowed_tools)
    return policy, agentic_policy_kwargs(policy)


def _record_agentic_step_gate(
    state: dict,
    *,
    step: str,
    policy: Any,
    result: Any,
) -> None:
    from framework.agentic_policy import (
        record_agentic_step_gate,
        validate_agentic_step_result,
    )

    validation = validate_agentic_step_result(policy, result)
    record_agentic_step_gate(
        workspace_path=state.get("workspace_path", ""),
        agent_id=_AGENT_ID,
        task_id=state.get("_task_id", ""),
        step=step,
        policy=policy,
        result=result,
        validation=validation,
    )
    if result.success and not validation.passed:
        raise RuntimeError(f"{step} agentic output gate failed: {validation.feedback}")


def _agentic_cwd(runtime: Any, cwd: str | None) -> str | None:
    if not cwd:
        return None
    if hasattr(runtime, "agentic_capabilities"):
        try:
            caps = runtime.agentic_capabilities()
            if not bool(getattr(caps, "cwd", False)):
                return None
        except Exception:
            return cwd
    return cwd


def _redact_personal_value(value: str) -> str:
    return "redacted" if str(value or "").strip() else ""


_NON_ACTIONABLE_REVIEW_CATEGORIES = {"review-process", "large-change"}
_NON_ACTIONABLE_REVIEW_PHRASES = (
    "diff was truncated",
    "truncated in diff",
    "not fully visible",
    "review focused on visible portion only",
    "single-file change is very large",
)


def _is_actionable_review_comment(comment: dict[str, Any]) -> bool:
    """Return True when a code-review comment can be acted on by the dev agent."""
    category = str(comment.get("category", "")).strip().lower()
    if category in _NON_ACTIONABLE_REVIEW_CATEGORIES:
        return False

    combined = " ".join(
        str(comment.get(key, "")).strip().lower()
        for key in ("message", "suggestion")
    )
    return not any(phrase in combined for phrase in _NON_ACTIONABLE_REVIEW_PHRASES)


def _review_comment_requires_fix(comment: dict[str, Any]) -> bool:
    """Return True when a review comment should block revision completion."""
    if not _is_actionable_review_comment(comment):
        return False

    return bool(issue_blocks_merge(comment))


def _git_worktree_changed_files(repo_path: str) -> list[str]:
    """Return tracked/untracked worktree files from git status."""
    if not repo_path or not os.path.isdir(repo_path):
        return []
    try:
        from framework.env_utils import build_isolated_git_env

        proc = subprocess.run(
            ["git", "status", "--short"],
            cwd=repo_path,
            env=build_isolated_git_env(scope="web-dev-worktree-status"),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    files: list[str] = []
    for line in proc.stdout.splitlines():
        path = line[3:].strip() if len(line) > 3 else line.strip()
        if path:
            files.append(path)
    return sorted(set(files))


def _git_branch_changed_files(repo_path: str, base_ref: str = "main") -> list[str]:
    """Return files changed on the current branch relative to *base_ref*."""
    if not repo_path or not os.path.isdir(repo_path):
        return []
    try:
        from framework.env_utils import build_isolated_git_env

        proc = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}..HEAD"],
            cwd=repo_path,
            env=build_isolated_git_env(scope="web-dev-branch-status"),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return sorted({line.strip() for line in proc.stdout.splitlines() if line.strip()})


def _workflow_changed_files(state: dict) -> list[str]:
    """Return the cumulative files changed by the current Web Dev workflow.

    Agentic backends differ in when they commit work and whether their tool
    result reports individual writes. The workflow-level evidence must include
    committed branch changes and uncommitted repair changes instead of relying
    on the latest node's local write list.
    """
    changed: list[str] = []
    seen: set[str] = set()

    def add(paths: Any) -> None:
        if not isinstance(paths, (list, tuple, set)):
            return
        for item in paths:
            path = str(item or "").strip()
            if path and path not in seen:
                seen.add(path)
                changed.append(path)

    add(state.get("changes_made", []))
    repo_path = state.get("repo_path", "") or ""
    base_ref = str(
        state.get("base_branch")
        or state.get("target_branch")
        or "main"
    )
    add(_git_branch_changed_files(repo_path, base_ref=base_ref))
    add(_git_worktree_changed_files(repo_path))
    return sorted(changed)


_SELF_ASSESS_SOURCE_SKIP_DIRS: frozenset[str] = frozenset({
    ".git",
    "node_modules",
    "dist",
    "build",
    "coverage",
    ".vite",
    ".next",
    "__pycache__",
})

_SELF_ASSESS_SOURCE_EXTS: frozenset[str] = frozenset({
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".vue",
    ".svelte",
    ".css",
    ".scss",
    ".html",
    ".json",
    ".mjs",
    ".cjs",
})


def _is_self_assess_source_file(path: str) -> bool:
    normalized = path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if any(part in _SELF_ASSESS_SOURCE_SKIP_DIRS for part in parts):
        return False
    if not parts:
        return False
    name = parts[-1].lower()
    if name in {
        "package.json",
        "vite.config.ts",
        "vite.config.js",
        "vitest.config.ts",
        "tailwind.config.js",
        "tsconfig.json",
        "index.html",
    }:
        return True
    return any(name.endswith(ext) for ext in _SELF_ASSESS_SOURCE_EXTS)


def _self_assess_source_priority(path: str) -> tuple[int, str]:
    lower = path.lower().replace("\\", "/")
    name = os.path.basename(lower)
    if name in {"app.tsx", "app.jsx", "app.ts", "app.js"}:
        return (0, lower)
    if name in {"main.tsx", "main.jsx", "index.tsx", "index.jsx"}:
        return (1, lower)
    if any(token in lower for token in ("route", "router", "page", "screen")):
        return (2, lower)
    if name in {"package.json", "vite.config.ts", "vite.config.js", "vitest.config.ts"}:
        return (3, lower)
    if "tailwind" in name or name.endswith(".css"):
        return (4, lower)
    return (5, lower)


def _expand_changed_source_files(repo_path: str, changed_files: list[str], *, limit: int = 120) -> list[str]:
    """Expand changed dirs like ``src/`` into concrete source/config files."""
    if not repo_path or not os.path.isdir(repo_path):
        return changed_files[:limit]

    discovered: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        rel = path.replace("\\", "/").strip("/")
        if not rel or rel in seen:
            return
        if not _is_self_assess_source_file(rel):
            return
        seen.add(rel)
        discovered.append(rel)

    for item in changed_files:
        rel = str(item or "").replace("\\", "/").strip()
        if not rel:
            continue
        abs_path = os.path.join(repo_path, rel.rstrip("/"))
        if os.path.isdir(abs_path):
            for root, dirnames, filenames in os.walk(abs_path):
                dirnames[:] = [
                    dirname for dirname in dirnames
                    if dirname not in _SELF_ASSESS_SOURCE_SKIP_DIRS
                ]
                for filename in filenames:
                    add(os.path.relpath(os.path.join(root, filename), repo_path))
                    if len(discovered) >= limit:
                        break
                if len(discovered) >= limit:
                    break
        elif os.path.isfile(abs_path):
            add(rel)
        elif not rel.endswith("/"):
            add(rel)

    discovered.sort(key=_self_assess_source_priority)
    return discovered[:limit]


def _build_self_assessment_source_evidence(repo_path: str, changed_files: list[str]) -> str:
    """Build deterministic repository evidence for self-assessment prompts."""
    expanded_files = _expand_changed_source_files(repo_path, changed_files)
    if not expanded_files:
        return "No source files could be deterministically enumerated."

    snippet_files = sorted(expanded_files, key=_self_assess_source_priority)[:14]
    snippets: list[str] = []
    for rel in snippet_files:
        abs_path = os.path.join(repo_path, rel)
        if not os.path.isfile(abs_path):
            continue
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as fh:
                content = fh.read(1800)
        except OSError:
            continue
        snippets.append(
            f"--- {rel} ---\n"
            f"{text_for_prompt(content, max_chars=1400, default='(empty file)')}"
        )

    file_list = "\n".join(f"- {path}" for path in expanded_files[:80])
    if len(expanded_files) > 80:
        file_list += f"\n- ...({len(expanded_files) - 80} more files)"
    snippet_text = "\n\n".join(snippets) if snippets else "No readable snippets."
    return text_for_prompt(
        f"Expanded changed source/config file inventory:\n{file_list}\n\n"
        f"Representative file snippets for verification:\n{snippet_text}",
        max_chars=18000,
        default="No source evidence available.",
    )


_FRONTEND_ENTRYPOINT_CANDIDATES: tuple[str, ...] = (
    "src/App.tsx",
    "src/App.jsx",
    "src/App.ts",
    "src/App.js",
    "src/main.tsx",
    "src/main.jsx",
    "src/index.tsx",
    "src/index.jsx",
    "app/page.tsx",
    "app/page.jsx",
    "pages/index.tsx",
    "pages/index.jsx",
)


def _repo_has_frontend_entrypoint(repo_path: str) -> bool:
    if not repo_path or not os.path.isdir(repo_path):
        return False
    return any(os.path.isfile(os.path.join(repo_path, rel)) for rel in _FRONTEND_ENTRYPOINT_CANDIDATES)


def _frontend_task_likely(state: dict, *, design_code_reference: str = "") -> bool:
    """Return True when a task likely needs a browser/frontend source tree."""
    signals: list[str] = []
    tech_stack = state.get("tech_stack") or []
    if isinstance(tech_stack, (list, tuple, set)):
        signals.extend(str(item) for item in tech_stack)
    else:
        signals.append(str(tech_stack))
    for key in (
        "user_request",
        "implementation_plan",
        "task_type",
        "classification",
        "work_type",
        "stitch_screen_name",
    ):
        signals.append(str(state.get(key, "")))
    if state.get("design_context") or state.get("design_spec"):
        signals.append("design frontend")
    if design_code_reference and design_code_reference != "N/A":
        signals.append(design_code_reference[:4000])

    repo_path = state.get("repo_path", "") or ""
    package_path = os.path.join(repo_path, "package.json")
    if os.path.isfile(package_path):
        try:
            with open(package_path, encoding="utf-8", errors="replace") as fh:
                signals.append(fh.read(4000))
        except OSError:
            pass

    combined = " ".join(signals).lower()
    return any(
        token in combined
        for token in (
            "react",
            "vite",
            "next.js",
            "nextjs",
            "vue",
            "svelte",
            "frontend",
            "front-end",
            "ui",
            "screen",
            "page",
            "tailwind",
            "tsx",
            "jsx",
        )
    )


def _write_repo_file_if_missing(repo_path: str, rel_path: str, content: str) -> bool:
    abs_path = os.path.join(repo_path, rel_path)
    if os.path.exists(abs_path):
        return False
    os.makedirs(os.path.dirname(abs_path) or repo_path, exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return True


def _bootstrap_minimal_frontend_scaffold(
    repo_path: str,
    state: dict,
    *,
    design_code_reference: str = "",
) -> list[str]:
    """Create a minimal frontend scaffold for greenfield browser apps.

    This is deliberately generic: it prepares the repo so every backend can
    immediately edit source files with Constellation tools instead of spending
    turns on interactive project generators. It never overwrites existing files
    and it does not implement task-specific UI or business behavior.
    """
    if not repo_path or not os.path.isdir(repo_path):
        return []
    if _repo_has_frontend_entrypoint(repo_path):
        return []
    if not _frontend_task_likely(state, design_code_reference=design_code_reference):
        return []

    files: dict[str, str] = {
        ".gitignore": """node_modules/
dist/
build/
.vite/
.env
*.local
coverage/
__pycache__/
screenshots/
docs/screenshots/
e2e/evidence/
FINAL_VERIFICATION.md
IMPLEMENTATION_EVIDENCE.md
VERIFICATION_SUMMARY.txt
*.log
""",
        "package.json": """{
  "name": "constellation-frontend-task",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build",
    "preview": "vite preview",
    "test": "vitest --run"
  },
  "dependencies": {
    "react": "^18.2.0",
    "react-dom": "^18.2.0"
  },
  "devDependencies": {
    "@testing-library/jest-dom": "^6.4.0",
    "@testing-library/react": "^14.2.0",
    "@types/react": "^18.2.55",
    "@types/react-dom": "^18.2.19",
    "@vitejs/plugin-react": "^4.2.1",
    "jsdom": "^24.0.0",
    "typescript": "^5.2.2",
    "vite": "^5.1.0",
    "vitest": "^1.3.1"
  }
}
""",
        "index.html": """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Constellation Frontend Task</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
""",
        "vite.config.ts": """import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
})
""",
        "vitest.config.ts": """import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
  },
})
""",
        "tsconfig.json": """{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["DOM", "DOM.Iterable", "ES2020"],
    "allowJs": false,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true,
    "strict": true,
    "forceConsistentCasingInFileNames": true,
    "module": "ESNext",
    "moduleResolution": "Node",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx"
  },
  "include": ["src"],
  "references": [{ "path": "./tsconfig.node.json" }]
}
""",
        "tsconfig.node.json": """{
  "compilerOptions": {
    "composite": true,
    "module": "ESNext",
    "moduleResolution": "Node",
    "allowSyntheticDefaultImports": true
  },
  "include": ["vite.config.ts", "vitest.config.ts"]
}
""",
        "src/main.tsx": """import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
""",
        "src/App.tsx": """export default function App() {
  return (
    <main className="app-shell" data-testid="app-root">
      <h1>Implementation scaffold</h1>
    </main>
  )
}
""",
        "src/index.css": """* {
  box-sizing: border-box;
}

body {
  margin: 0;
  min-width: 320px;
  min-height: 100vh;
  font-family: system-ui, sans-serif;
}

.app-shell {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
}
""",
        "src/App.test.tsx": """import '@testing-library/jest-dom/vitest'
import { render, screen } from '@testing-library/react'
import App from './App'

it('renders the application root', () => {
  render(<App />)
  expect(screen.getByTestId('app-root')).toBeInTheDocument()
})
""",
    }

    created: list[str] = []
    for rel_path, content in files.items():
        try:
            if _write_repo_file_if_missing(repo_path, rel_path, content):
                created.append(rel_path)
        except OSError:
            continue
    return created


def _summarize_validation_commands(data: dict) -> list[dict[str, Any]]:
    """Return compact validation command summaries for agent.log."""
    summaries: list[dict[str, Any]] = []
    for command in data.get("commands") or []:
        parts = command.get("command") or []
        summaries.append(
            {
                "command": " ".join(str(part) for part in parts),
                "returncode": command.get("returncode"),
                "duration_seconds": command.get("duration_seconds"),
            }
        )
    return summaries


def _call_boundary_tool(state: dict, tool_name: str, args: dict) -> dict:
    """Call a boundary agent tool via the global ToolRegistry.

    Returns the parsed JSON payload or an error dict.
    """
    from framework.tools.registry import get_registry

    registry = get_registry()
    tool_args = dict(args)
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    permissions = metadata.get("permissions")
    if isinstance(permissions, dict) and not isinstance(tool_args.get("permissions"), dict):
        tool_args["permissions"] = permissions
    try:
        result_str = registry.execute_sync(tool_name, tool_args)
        return json.loads(result_str) if result_str else {}
    except Exception as exc:
        print(f"[{_AGENT_ID}] Tool {tool_name} failed: {exc}")
        return {"error": str(exc)}


def _is_screenshot_required(state: dict) -> bool:
    """Return whether this task must produce PNG implementation screenshots."""
    definition_of_done = state.get("definition_of_done") or {}
    if isinstance(definition_of_done, dict) and "screenshot_required" in definition_of_done:
        return bool(definition_of_done.get("screenshot_required"))

    if state.get("design_context") or state.get("design_spec"):
        return True
    if state.get("stitch_screen_id") or state.get("stitch_screen_name"):
        return True

    task_signals = " ".join(
        str(state.get(key, "")).lower()
        for key in ("task_type", "classification", "work_type")
    )
    return any(token in task_signals for token in ("ui", "frontend", "front-end", "visual", "design"))


def _rendered_page_has_content(metrics: dict[str, Any]) -> bool:
    """Return True when the browser page shows enough evidence of real rendering."""
    root_children = int(metrics.get("rootChildren") or 0)
    body_children = int(metrics.get("bodyChildren") or 0)
    visible_text_chars = int(metrics.get("visibleTextChars") or 0)
    body_width = int(metrics.get("bodyWidth") or 0)
    body_height = int(metrics.get("bodyHeight") or 0)
    return (
        (root_children > 0 or body_children > 1 or visible_text_chars >= 20)
        and body_width > 0
        and body_height > 0
    )


_ICON_LIGATURE_TOKENS = (
    "arrow_forward",
    "arrow_back",
    "arrow_upward",
    "arrow_downward",
    "chevron_right",
    "chevron_left",
    "navigate_next",
    "navigate_before",
    "expand_more",
    "expand_less",
    "close",
    "menu",
    "search",
)


def _strip_icon_scan_comments(content: str, ext: str) -> str:
    """Remove comments before scanning for runtime icon ligature usage."""
    if ext == ".html":
        return re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    if ext in {".js", ".jsx", ".ts", ".tsx"}:
        without_block_comments = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
        return re.sub(r"(?m)(^|[ \t])//.*$", r"\1", without_block_comments)
    return content


def _detect_fragile_icon_font_usage(repo_path: str) -> dict[str, Any]:
    """Detect icon-font ligature patterns that render unreliably in containers."""
    findings: dict[str, Any] = {
        "issues": [],
        "files": [],
        "icon_tokens": [],
        "uses_material_icon_class": False,
        "uses_remote_material_font": False,
    }
    if not repo_path or not os.path.isdir(repo_path):
        return findings

    text_exts = {".html", ".css", ".scss", ".sass", ".less", ".js", ".jsx", ".ts", ".tsx"}
    markup_exts = {".html", ".js", ".jsx", ".ts", ".tsx"}
    ignored_dirs = {".git", "node_modules", "dist", "build", ".next", "coverage"}
    risky_files: set[str] = set()
    icon_tokens: set[str] = set()

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [name for name in dirs if name not in ignored_dirs]
        for filename in files:
            if os.path.splitext(filename)[1].lower() not in text_exts:
                continue
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, repo_path)
            ext = os.path.splitext(filename)[1].lower()
            try:
                with open(full_path, encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
            except OSError:
                continue
            scan_content = _strip_icon_scan_comments(content, ext)
            lowered = scan_content.lower()

            if "material-symbols" in lowered or "material-icons" in lowered:
                # A stylesheet may define or leave behind the class selector
                # without any runtime markup using the icon font. Only markup
                # usage can render ligature text in screenshots.
                if ext in markup_exts:
                    findings["uses_material_icon_class"] = True
                    risky_files.add(rel_path)

            if (
                "fonts.googleapis.com" in lowered
                and ("material+symbols" in lowered or "material+icons" in lowered or "icon?family=material+icons" in lowered)
            ):
                findings["uses_remote_material_font"] = True
                risky_files.add(rel_path)

            for token in _ICON_LIGATURE_TOKENS:
                if ext in markup_exts and re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", lowered):
                    icon_tokens.add(token)
                    risky_files.add(rel_path)

    findings["files"] = sorted(risky_files)
    findings["icon_tokens"] = sorted(icon_tokens)

    if findings["uses_material_icon_class"] or findings["icon_tokens"]:
        preview = ", ".join(findings["icon_tokens"][:3]) or "material icon ligatures"
        findings["issues"].append(
            "Fragile icon font usage detected "
            f"({preview}) in {', '.join(findings['files'][:4])}. "
            "Replace icon-font ligatures with inline SVG or a local React icon component so container screenshots never show icon names as text."
        )
    if findings["uses_material_icon_class"] and not findings["uses_remote_material_font"]:
        findings["issues"].append(
            "Material icon classes are present without a matching icon font stylesheet. "
            "The page can render icon names as plain text."
        )

    return findings


def _git_commit_all_pending(repo_path: str, jira_key: str) -> list[str]:
    """Stage all pending changes and commit if anything is staged.

    Called inside create_pr before scm_push to ensure every file written by
    the agentic implement_changes loop is committed — even when the LLM only
    ran 'git commit <specific-file>' instead of 'git add -A && git commit'.
    """
    if not repo_path or not os.path.isdir(repo_path):
        return []
    try:
        import subprocess
        from framework.env_utils import build_isolated_git_env
        git_env = build_isolated_git_env(scope="web-dev-commit")

        # Stage all untracked/modified files
        subprocess.run(
            ["git", "add", "-A"],
            cwd=repo_path, env=git_env,
            capture_output=True, text=True, timeout=30,
        )

        # Check if there is anything staged
        status_result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo_path, env=git_env,
            capture_output=True, text=True, timeout=10,
        )
        staged_files = [f for f in status_result.stdout.strip().splitlines() if f]

        if staged_files:
            commit_msg = f"feat({jira_key or 'task'}): implement changes"
            r = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=repo_path, env=git_env,
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                print(f"[{_AGENT_ID}] committed {len(staged_files)} pending file(s): {staged_files[:8]}")
            else:
                print(f"[{_AGENT_ID}] commit failed: {r.stderr.strip()[:200]}")
            return staged_files
        else:
            # Confirm there is at least one commit on the branch, and list its changed files
            log_result = subprocess.run(
                ["git", "log", "--oneline", "-1"],
                cwd=repo_path, env=git_env,
                capture_output=True, text=True, timeout=10,
            )
            print(f"[{_AGENT_ID}] no pending changes; last commit: {log_result.stdout.strip()[:120]!r}")
            # Get files from HEAD commit (agent already committed during run_agentic)
            diff_result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~1..HEAD"],
                cwd=repo_path, env=git_env,
                capture_output=True, text=True, timeout=10,
            )
            if diff_result.returncode == 0:
                return [f for f in diff_result.stdout.strip().splitlines() if f]
            return []
    except Exception as exc:
        print(f"[{_AGENT_ID}] _git_commit_all_pending error (non-fatal): {exc}")
        return []


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------

async def prepare_jira(state: dict) -> dict:
    """Update Jira before implementation starts."""
    log = _logger(state)
    log.node("prepare_jira")
    jira_context = state.get("jira_context", {})
    jira_key = (
        jira_context.get("key")
        or jira_context.get("ticket_key")
        or state.get("jira_key", "")
    )
    log.debug("prepare_jira", jira_key=jira_key)
    print(f"[{_AGENT_ID}] prepare_jira: jira_key={jira_key!r}")

    if not jira_key:
        log.info("prepare_jira skipped — no jira_key")
        return {"jira_prepared": False, "jira_prepare_skipped": "no_jira_key"}

    # Resolve original status for rollback
    original_status = ""
    original_assignee = ""
    if isinstance(jira_context, dict):
        fields = jira_context.get("fields", jira_context)
        original_status = (
            fields.get("status", {}).get("name", "")
            if isinstance(fields.get("status"), dict)
            else str(fields.get("status", ""))
        )
        assignee = fields.get("assignee") or {}
        original_assignee = (
            assignee.get("emailAddress", assignee.get("displayName", ""))
            if isinstance(assignee, dict)
            else str(assignee)
        )

    # Resolve token user
    token_user = ""
    token_user_account_id = ""
    task_id = state.get("_task_id", "")
    token_user_result = _call_boundary_tool(state, "jira_get_token_user", {"task_id": task_id})
    if not token_user_result.get("error"):
        user_data = token_user_result.get("user", {})
        token_user = user_data.get("emailAddress", user_data.get("displayName", ""))
        token_user_account_id = user_data.get("accountId", "")

    # Transition to "In Progress" if not already
    if original_status.lower() not in ("in progress", "in development", "in dev"):
        transitions_result = _call_boundary_tool(
            state, "jira_list_transitions", {"ticket_key": jira_key, "task_id": task_id}
        )
        transitions = transitions_result.get("transitions", [])
        _IN_PROGRESS_NAMES = {
            "in progress", "start progress", "in development", "in dev",
            "start development", "start", "begin", "begin work",
        }
        in_progress_match = next(
            (t for t in transitions
             if isinstance(t, dict) and t.get("name", "").lower() in _IN_PROGRESS_NAMES),
            None,
        )
        if in_progress_match:
            _call_boundary_tool(
                state, "jira_transition",
                {"ticket_key": jira_key, "transition_name": in_progress_match["name"],
                 "task_id": task_id},
            )
        else:
            avail = [t.get("name") for t in transitions if isinstance(t, dict)]
            print(f"[{_AGENT_ID}] Cannot transition {jira_key} to In Progress; available: {avail}")

    # Update assignee to token user (use accountId for Jira Cloud)
    if token_user_account_id:
        log.info(
            "assigning jira ticket to token user in prepare",
            jira_key=jira_key,
            account_id_present=True,
        )
        _call_boundary_tool(
            state, "jira_update",
            {"ticket_key": jira_key,
             "fields": {"assignee": {"accountId": token_user_account_id}},
             "task_id": task_id},
        )
    elif token_user and token_user != original_assignee:
        # Fallback for Jira Server (uses emailAddress)
        _call_boundary_tool(
            state, "jira_update",
            {"ticket_key": jira_key, "fields": {"assignee": {"emailAddress": token_user}},
             "task_id": task_id},
        )

    # Add pickup comment with task_id
    _task_id = state.get("_task_id", "unknown")
    _call_boundary_tool(
        state, "jira_comment",
        {
            "ticket_key": jira_key,
            "comment": (
                f"🤖 Development agent (web-dev) has picked up this ticket.\n"
                f"Task ID: {_task_id}\n"
                f"Assignee: {'token user' if token_user else 'unassigned'}\n"
                f"Status: In Progress"
            ),
            "task_id": task_id,
        },
    )
    log.info(
        "prepare_jira complete",
        jira_key=jira_key,
        token_user_present=bool(token_user),
    )

    # Write jira-prepare-log.json
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        import time as _time
        agent_dir = os.path.join(workspace_path, _AGENT_ID)
        os.makedirs(agent_dir, exist_ok=True)
        try:
            log_file = os.path.join(agent_dir, "jira-prepare-log.json")
            with open(log_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "web-dev",
                        "step": "prepare_jira",
                        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": {
                        "jira_key": jira_key,
                        "jira_original_status": original_status,
                        "jira_original_assignee": _redact_personal_value(original_assignee),
                        "jira_original_assignee_present": bool(original_assignee),
                        "jira_token_user": _redact_personal_value(token_user),
                        "jira_token_user_present": bool(token_user),
                        "jira_prepared": True,
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    return {
        "jira_prepared": True,
        "jira_original_status": original_status,
        "jira_original_assignee": _redact_personal_value(original_assignee),
        "jira_original_assignee_present": bool(original_assignee),
        "jira_token_user": _redact_personal_value(token_user),
        "jira_token_user_present": bool(token_user),
    }

async def setup_workspace(state: dict) -> dict:
    """Create a working branch in the cloned repository."""
    runtime = state.get("_runtime")
    log = _logger(state)
    log.node("setup_workspace")
    repo_url = state.get("repo_url", "")
    repo_path = state.get("repo_path", "")
    workspace_path = state.get("workspace_path", "")
    branch_name = state.get("branch_name", "")
    existing_branch = state.get("existing_branch", "")
    revision_mode = bool(state.get("revision_mode", False))
    task_id = state.get("_task_id", "unknown")
    log.debug("setup_workspace", repo_path=repo_path)
    print(f"[{_AGENT_ID}] setup_workspace: repo_path={repo_path!r} workspace_path={workspace_path!r}")

    # Use workspace_path from Team Lead; only fall back to artifacts/ if missing
    if not workspace_path:
        workspace_path = os.path.join(
            os.path.abspath(os.environ.get("ARTIFACT_ROOT", "artifacts")),
            f"workspace-{task_id}",
        )
        os.makedirs(workspace_path, exist_ok=True)
    if not repo_path:
        repo_path = os.path.join(workspace_path, "repo")

    if revision_mode:
        branch_name = existing_branch or branch_name

    # Fail fast if repo does not exist — Team Lead must have cloned it first
    if not os.path.isdir(repo_path):
        raise RuntimeError(
            f"[{_AGENT_ID}] Repo not found at {repo_path!r}. "
            "Team Lead must clone the repo before dispatching to Web Dev."
        )

    if revision_mode and not branch_name:
        raise RuntimeError(
            f"[{_AGENT_ID}] Revision mode requires an existing branch name to reuse."
        )

    # Derive branch name: use provided value, then LLM, then Jira-key fallback
    if not branch_name and runtime:
        from agents.web_dev.prompts import SETUP_SYSTEM, SETUP_TEMPLATE
        jira_context = state.get("jira_context", {})
        prompt = SETUP_TEMPLATE.format(
            user_request=state.get("user_request", ""),
            repo_url=repo_url,
            jira_context=json.dumps(jira_context, ensure_ascii=False) if jira_context else "N/A",
        )
        result = runtime.run(prompt, system_prompt=SETUP_SYSTEM,
                             plugin_manager=state.get("_plugin_manager"))
        data = _safe_json(result.get("raw_response", ""), fallback={})
        branch_name = data.get("branch_name", "")

    # Derive branch name from Jira key when LLM result is unavailable
    if not branch_name:
        jira_key_raw = (
            (state.get("jira_context") or {}).get("key", "")
            or state.get("jira_key", "")
        ).upper()
        task_suffix = state.get("_task_id", "task")[:8]
        if jira_key_raw:
            branch_name = f"feature/{jira_key_raw}-{task_suffix}"
        else:
            branch_name = f"feature/{task_suffix}"

    local_branch_exists = False
    if repo_path and os.path.isdir(repo_path) and branch_name:
        import subprocess
        from framework.env_utils import build_isolated_git_env

        git_env = build_isolated_git_env("web-dev-setup-local-branch")
        exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            cwd=repo_path,
            env=git_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        local_branch_exists = exists.returncode == 0

    # -- Check remote branches and open PR source branches for conflicts; add _<n> suffix when taken --
    # Must not delete or alter existing remote branches or PRs.
    if branch_name and repo_url and not local_branch_exists and not revision_mode:
        remote_result = _call_boundary_tool(state, "scm_list_branches", {"repo_url": repo_url})
        remote_branch_names = {
            candidate
            for b in remote_result.get("branches", [])
            for candidate in [
                b.get("displayId", ""),
                b.get("name", ""),
                str(b.get("id", "")).replace("refs/heads/", ""),
            ]
            if candidate
        }
        pr_result = _call_boundary_tool(state, "scm_list_prs", {"repo_url": repo_url, "state": "open"})
        reserved_pr_branches = {
            str(pr.get("fromBranch") or pr.get("fromRef") or pr.get("sourceBranch") or "").strip()
            for pr in pr_result.get("prs", [])
            if str(pr.get("fromBranch") or pr.get("fromRef") or pr.get("sourceBranch") or "").strip()
        }
        reserved_names = remote_branch_names | reserved_pr_branches
        if branch_name in reserved_names:
            base_name = branch_name
            n = 2
            while f"{base_name}_{n}" in reserved_names:
                n += 1
            new_name = f"{base_name}_{n}"
            print(
                f"[{_AGENT_ID}] setup_workspace: branch {branch_name!r} is already reserved "
                f"by a remote branch or open PR, using {new_name!r} to avoid conflict"
            )
            branch_name = new_name

    # Actually create / checkout the branch in the cloned repo
    branch_created = False
    if repo_path and os.path.isdir(repo_path) and branch_name:
        import subprocess
        from framework.env_utils import build_isolated_git_env
        git_env = build_isolated_git_env("web-dev-setup")
        if revision_mode:
            if local_branch_exists:
                r = subprocess.run(
                    ["git", "checkout", branch_name],
                    cwd=repo_path, env=git_env,
                    capture_output=True, text=True, timeout=30,
                )
                if r.returncode != 0:
                    raise RuntimeError(
                        f"[{_AGENT_ID}] Failed to switch to existing branch {branch_name!r}: {r.stderr.strip()[:200]}"
                    )
                branch_created = True
                print(f"[{_AGENT_ID}] setup_workspace: reusing existing branch {branch_name!r}")
            else:
                fetch_result = subprocess.run(
                    ["git", "fetch", "origin", branch_name],
                    cwd=repo_path, env=git_env,
                    capture_output=True, text=True, timeout=60,
                )
                if fetch_result.returncode != 0:
                    raise RuntimeError(
                        f"[{_AGENT_ID}] Revision branch {branch_name!r} is missing locally and could not be fetched: {fetch_result.stderr.strip()[:200]}"
                    )
                checkout_result = subprocess.run(
                    ["git", "checkout", "-B", branch_name, f"origin/{branch_name}"],
                    cwd=repo_path, env=git_env,
                    capture_output=True, text=True, timeout=30,
                )
                if checkout_result.returncode != 0:
                    raise RuntimeError(
                        f"[{_AGENT_ID}] Failed to track existing revision branch {branch_name!r}: {checkout_result.stderr.strip()[:200]}"
                    )
                branch_created = True
                print(f"[{_AGENT_ID}] setup_workspace: fetched and reused branch {branch_name!r}")
        else:
            r = subprocess.run(
                ["git", "checkout", "-b", branch_name],
                cwd=repo_path, env=git_env,
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                branch_created = True
                print(f"[{_AGENT_ID}] setup_workspace: created branch {branch_name!r}")
            else:
                # Branch might already exist — try switching to it
                r2 = subprocess.run(
                    ["git", "checkout", branch_name],
                    cwd=repo_path, env=git_env,
                    capture_output=True, text=True, timeout=30,
                )
                if r2.returncode == 0:
                    branch_created = True
                    print(f"[{_AGENT_ID}] setup_workspace: switched to existing branch {branch_name!r}")
                else:
                    print(f"[{_AGENT_ID}] setup_workspace: git checkout failed: {r2.stderr.strip()[:200]}")
                    raise RuntimeError(
                        f"[{_AGENT_ID}] Failed to create/switch branch {branch_name!r}: {r2.stderr.strip()[:200]}"
                    )

    # Write git setup log
    if workspace_path:
        agent_dir = os.path.join(workspace_path, _AGENT_ID)
        os.makedirs(agent_dir, exist_ok=True)
        try:
            import time as _time
            log_file = os.path.join(agent_dir, "git-setup-log.json")
            with open(log_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "web-dev",
                        "step": "setup_workspace",
                        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": {
                        "repo_url": repo_url,
                        "repo_path": repo_path,
                        "branch_name": branch_name or "feature/task",
                        "repo_exists": os.path.isdir(repo_path),
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    return {
        "workspace_path": workspace_path,
        "repo_path": repo_path,
        "branch_name": branch_name or "feature/task",
        "branch_created": branch_created,
    }


async def analyze_task(state: dict) -> dict:
    """Understand requirements and produce an implementation plan."""
    import time as _time
    log = _logger(state)
    log.node("analyze_task")
    _record_timeline_step(
        state,
        step_key="wd.drafting_plan",
        title="Web Dev drafting plan",
        summary_template="Web Dev is drafting the implementation plan.",
    )
    print(f"[{_AGENT_ID}] analyze_task: building implementation plan")

    workspace_path = state.get("workspace_path", "")
    analysis = text_for_prompt(
        state.get("analysis") or state.get("analysis_summary") or state.get("user_request", ""),
        max_chars=3000,
        default="",
    )

    # Try to load Team Lead's delivery-plan.json for structured plan data
    delivery_plan: dict = {}
    if workspace_path:
        plan_path = os.path.join(workspace_path, "team-lead", "delivery-plan.json")
        try:
            with open(plan_path, encoding="utf-8") as fh:
                doc = json.load(fh)
                delivery_plan = compact_delivery_plan(doc.get("data", doc))
                print(f"[{_AGENT_ID}] Loaded delivery-plan.json from {plan_path}")
        except (OSError, json.JSONDecodeError):
            pass

    # Build rich plan string
    plan_parts = []
    if analysis:
        plan_parts.append(analysis)
    if delivery_plan:
        plan_parts.append(
            "\nDelivery plan:\n"
            + text_for_prompt(
                delivery_plan,
                max_chars=5000,
                default="N/A",
            )
        )

    # Also load Jira ticket for acceptance criteria
    if workspace_path:
        jira_path = os.path.join(workspace_path, "team-lead", "jira-ticket.json")
        try:
            with open(jira_path, encoding="utf-8") as fh:
                doc = json.load(fh)
                jira_data = doc.get("data", doc)
                summary = jira_data.get("summary", "") or (jira_data.get("fields") or {}).get("summary", "")
                if summary:
                    plan_parts.append(f"\nJira ticket summary: {summary}")
        except (OSError, json.JSONDecodeError):
            pass

    plan = text_for_prompt("\n".join(plan_parts) if plan_parts else analysis, max_chars=8000, default="")
    structured_plan = {
        "implementation_steps": [],
        "test_plan": [],
        "risks": [],
    }
    if isinstance(delivery_plan, dict) and delivery_plan.get("steps"):
        structured_plan["implementation_steps"] = [
            str(step.get("action") or step.get("description") or step)
            for step in delivery_plan.get("steps", [])
            if isinstance(step, dict) or step
        ]
    if not structured_plan["implementation_steps"] and plan:
        structured_plan["implementation_steps"] = [plan]
    structured_plan["test_plan"] = [
        "Run deterministic install, build, and test validation before PR creation."
    ]
    structured_plan["risks"] = [
        "External service credentials or repository baseline health may affect validation."
    ]

    from framework.validation_gates import validate_implementation_plan
    gate_result = validate_implementation_plan(structured_plan)
    if not gate_result.passed:
        raise RuntimeError(f"Implementation plan gate failed: {gate_result.feedback}")

    # Write implementation-plan.json to workspace for auditability
    if workspace_path:
        agent_dir = os.path.join(workspace_path, _AGENT_ID)
        os.makedirs(agent_dir, exist_ok=True)
        try:
            plan_file = os.path.join(agent_dir, "implementation-plan.json")
            with open(plan_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "web-dev",
                        "step": "analyze_task",
                        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": {
                        "implementation_plan": plan,
                        "structured_plan": structured_plan,
                        "delivery_plan_loaded": bool(delivery_plan),
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    _record_timeline_step(
        state,
        step_key="wd.drafting_plan",
        title="Web Dev drafting plan",
        lifecycle_state=LIFECYCLE_DONE,
        summary_template="Web Dev drafted the implementation plan with {step_count} implementation steps.",
        summary_facts={"step_count": len(structured_plan.get("implementation_steps", []))},
    )

    return {
        "implementation_plan": plan,
        "implementation_plan_details": structured_plan,
    }


async def implement_changes(state: dict) -> dict:
    """Write code based on the implementation plan."""
    runtime = state.get("_runtime")
    log = _logger(state)
    revision_round = int(state.get("revision_round", 0) or 0)
    revision_mode = bool(state.get("revision_mode") or state.get("revision_feedback"))
    step_round = max(revision_round - 1, 0) if revision_mode else 0
    step_key = "wd.addressing_feedback" if revision_mode else "wd.implementing"
    title = "Web Dev addressing review feedback" if revision_mode else "Web Dev implementing changes"
    log.node("implement_changes", repo_path=state.get("repo_path", ""),
             branch=state.get("branch_name", ""))
    _record_timeline_step(
        state,
        step_key=step_key,
        title=title,
        summary_template="Web Dev is updating the codebase for the assigned task.",
        round=step_round,
        conditional=revision_mode,
    )
    if not runtime:
        # Unit-test / no-runtime path
        _record_timeline_step(
            state,
            step_key=step_key,
            title=title,
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Web Dev completed the implementation step in test mode.",
            round=step_round,
            conditional=revision_mode,
        )
        return {
            "changes_made": [],
            "implementation_summary": "Changes implemented (no runtime — test mode).",
            "agentic_success": True,
        }

    from agents.web_dev.prompts import IMPLEMENT_SYSTEM, IMPLEMENT_TEMPLATE

    repo_path = state.get("repo_path", "")
    branch_name = state.get("branch_name", "")
    changed_before = _repo_changed_file_set(repo_path)

    jira_ctx = state.get("jira_context", {})
    # Truncate: full Jira REST response can be 200KB+ — keep only essential fields
    jira_for_prompt = _summarize_jira_context(jira_ctx)
    impl_plan = text_for_prompt(
        state.get("implementation_plan", ""),
        max_chars=4000,
        default="N/A",
    )

    # Load design HTML code from workspace for component reference
    _design_code_ref = "N/A"
    _design_code_path = state.get("design_code_path", "")
    _workspace_path = state.get("workspace_path", "")
    # Prefer ui-design/stitch/code.html, fallback to team-lead/design-code.html
    if not _design_code_path and _workspace_path:
        _stitch_code = os.path.join(_workspace_path, "ui-design", "stitch", "code.html")
        _legacy_code = os.path.join(_workspace_path, "team-lead", "design-code.html")
        _design_code_path = _stitch_code if os.path.isfile(_stitch_code) else _legacy_code
    if _design_code_path and os.path.isfile(_design_code_path):
        try:
            with open(_design_code_path, encoding="utf-8") as _f:
                _design_code_ref = text_for_prompt(
                    _f.read(),
                    max_chars=int(os.environ.get("WEB_DEV_DESIGN_HTML_PROMPT_MAX_CHARS", "12000") or "12000"),
                    default="N/A",
                )
        except Exception:
            pass

    # Load design spec markdown (typography/colors/spacing) for reference
    _design_spec_md = "N/A"
    if _workspace_path:
        _stitch_md_path = os.path.join(_workspace_path, "ui-design", "stitch", "DESIGN.md")
        _legacy_md_path = os.path.join(_workspace_path, "team-lead", "design-spec.md")
        _design_spec_md_path = state.get("design_md_path", "") or (
            _stitch_md_path if os.path.isfile(_stitch_md_path) else _legacy_md_path
        )
        if os.path.isfile(_design_spec_md_path):
            try:
                with open(_design_spec_md_path, encoding="utf-8") as _f:
                    _design_spec_md = text_for_prompt(
                        _f.read(),
                        max_chars=int(os.environ.get("WEB_DEV_DESIGN_SPEC_PROMPT_MAX_CHARS", "6000") or "6000"),
                        default="N/A",
                    )
            except Exception:
                pass

    bootstrap_files = _bootstrap_minimal_frontend_scaffold(
        repo_path,
        state,
        design_code_reference=_design_code_ref,
    )
    if bootstrap_files:
        log.info(
            "prepared minimal frontend scaffold",
            files=bootstrap_files[:20],
            file_count=len(bootstrap_files),
        )

    # Pre-scan repo so LLM doesn't waste turns on exploration
    _repo_path = repo_path
    _repo_files_section: str
    if _repo_path and os.path.isdir(_repo_path):
        try:
            import glob as _glob_mod
            _all = _glob_mod.glob("**/*", root_dir=_repo_path, recursive=True)
            _files = sorted(f for f in _all if os.path.isfile(os.path.join(_repo_path, f)))[:60]
            if _files:
                _repo_files_section = "\n".join(f"  {f}" for f in _files)
            else:
                _repo_files_section = (
                    "  (EMPTY — only README.md or no files). "
                    "You MUST create all project files from scratch starting in turn 1."
                )
        except Exception:
            _repo_files_section = "  (could not list files)"
    else:
        _repo_files_section = "  (repo path not available)"

    prompt = IMPLEMENT_TEMPLATE.format(
        user_request=state.get("user_request", ""),
        repo_path=state.get("repo_path", ""),
        branch_name=state.get("branch_name", "feature/task"),
        tech_stack=", ".join(state.get("tech_stack") or []) or "not specified",
        stitch_screen_name=state.get("stitch_screen_name", "not specified"),
        repo_files=_repo_files_section,
        implementation_plan=impl_plan,
        jira_context=jira_for_prompt,
        design_context=text_for_prompt(state.get("design_context", "N/A"), max_chars=2000, default="N/A"),
        design_code_reference=_design_code_ref,
        design_spec_markdown=_design_spec_md,
        skill_context=text_for_prompt(state.get("skill_context", ""), max_chars=4000, default=""),
        memory_context=text_for_prompt(state.get("memory_context", ""), max_chars=3000, default=""),
    )
    if bootstrap_files:
        prompt += (
            "\n\nWorkflow scaffold note:\n"
            "Constellation prepared a generic minimal frontend scaffold because "
            "the repository did not contain a browser app entrypoint. Replace "
            "the placeholder App/source content with the requested implementation; "
            "do not leave scaffold placeholder text in the final UI.\n"
            "Prepared scaffold files:\n"
            + "\n".join(f"- {path}" for path in bootstrap_files)
            + "\n"
        )

    # Inject coding standards for consistent implementation (same standards used by Code Review)
    try:
        from framework.standards_loader import (
            detect_tech_stack_from_repo,
            format_standards_for_prompt,
            load_standards,
        )
        tech_stack_detected = state.get("tech_stack") or []
        if not tech_stack_detected and _repo_path:
            tech_stack_detected = detect_tech_stack_from_repo(_repo_path)
        std_rules = load_standards(tech_stack=tech_stack_detected, agent_role="development")
        std_text = format_standards_for_prompt(std_rules, agent_role="development")
        if std_text:
            prompt += f"\n\n{std_text}\n"
    except Exception:
        pass

    # Use the configured agentic backend through the Constellation-controlled
    # tool surface derived from the task execution contract.
    log.info(
        "implement_changes started",
        repo_path=repo_path,
        branch=branch_name,
        jira_local_folder=state.get("jira_local_folder", ""),
        design_local_folder=state.get("design_local_folder", ""),
    )
    policy, policy_kwargs = _agentic_policy_for_state(state, runtime)
    def _implementation_progress(message: Any) -> None:
        log.info("implement_changes progress", progress_message=str(message)[:300])

    print(f"[{_AGENT_ID}] implement_changes: repo_path={state.get('repo_path', '')!r} backend={policy.backend!r}")
    implement_max_turns = _state_env_int(
        state,
        "implement_max_turns",
        "implementMaxTurns",
        "WEB_DEV_IMPLEMENT_MAX_TURNS",
        80,
        minimum=20,
        maximum=300,
    )
    implement_timeout = _state_env_int(
        state,
        "implement_timeout_seconds",
        "implementTimeoutSeconds",
        "WEB_DEV_IMPLEMENT_TIMEOUT_SECONDS",
        2400,
        minimum=300,
        maximum=7200,
    )
    result = runtime.run_agentic(
        task=prompt,
        system_prompt=IMPLEMENT_SYSTEM,
        cwd=_agentic_cwd(runtime, state.get("repo_path") or None),
        max_turns=implement_max_turns,
        timeout=implement_timeout,
        on_progress=_implementation_progress,
        plugin_manager=state.get("_plugin_manager"),
        **policy_kwargs,
    )
    _record_agentic_step_gate(state, step="implement_changes", policy=policy, result=result)
    changed_after = _repo_changed_file_set(repo_path)
    changed_files = sorted(changed_after)
    new_files = sorted(changed_after - changed_before)
    frontend_entrypoint_missing = (
        _frontend_task_likely(state, design_code_reference=_design_code_ref)
        and not _repo_has_frontend_entrypoint(repo_path)
    )
    log.info(
        "implement_changes result",
        success=result.success,
        turns=result.turns_used,
        files_changed=len(changed_files),
        new_files=len(new_files),
        files=changed_files[:12],
        frontend_entrypoint_missing=frontend_entrypoint_missing,
    )
    if new_files:
        log.debug("implement_changes new files", files=new_files[:20])
    if result.summary:
        log.debug("implement_changes summary", summary=result.summary[:500])
    print(f"[{_AGENT_ID}] implement_changes done: success={result.success} turns={result.turns_used} summary={result.summary[:300]!r}")

    if frontend_entrypoint_missing:
        backend_label = _runtime_backend_label(runtime, result)
        raise RuntimeError(
            "implement_changes failed — "
            f"{backend_label} did not create a frontend source entrypoint "
            "(for example src/App.tsx, src/main.tsx, app/page.tsx, or pages/index.tsx). "
            "Configuration-only changes are not enough for a frontend task."
        )

    if not result.success:
        # Before failing, check if the agentic CLI committed code despite the
        # error/timeout. Some backends commit changes before a later validation
        # step fails or times out; do not discard committed work in that case.
        _partial_changes_exist = bool(changed_after)
        try:
            import subprocess as _sp
            from framework.env_utils import build_isolated_git_env as _bge
            _ge = _bge(scope="web-dev-impl-check")
            _diff = _sp.run(
                ["git", "diff", "--name-only", "main..HEAD"],
                cwd=state.get("repo_path", ""), capture_output=True, text=True,
                timeout=10, env=_ge,
            )
            _partial_changes_exist = (
                _partial_changes_exist
                or (_diff.returncode == 0 and bool(_diff.stdout.strip()))
            )
        except Exception:
            pass
        if _partial_changes_exist and _agentic_failure_allows_partial_progress(result.summary):
            print(f"[{_AGENT_ID}] implement_changes: agentic error ({result.summary[:200]!r}) "
                  f"but repository changes exist — proceeding to deterministic validation")
            impl_summary = _agentic_repair_summary(result, "implementation")
        else:
            backend_label = _runtime_backend_label(runtime, result)
            raise RuntimeError(
                f"implement_changes failed — {backend_label} returned error: {result.summary[:500]}"
            )
    else:
        impl_summary = result.summary

    # With native tools, we can't track individual file writes from tool_calls.
    # changes_made is populated from git diff in create_pr via _git_commit_all_pending.

    # Validation gate: ensure at least some files were changed
    from framework.validation_gates import validate_files_changed
    gate_result = validate_files_changed(state.get("repo_path", ""))
    if not gate_result.passed and "No file changes detected" in gate_result.feedback:
        log.error("validate_files_changed gate failed", feedback=gate_result.feedback)
        raise RuntimeError(f"Implementation produced no file changes: {gate_result.feedback}")
    elif not gate_result.passed:
        log.warn("validate_files_changed gate inconclusive", feedback=gate_result.feedback)

    _record_timeline_step(
        state,
        step_key=step_key,
        title=title,
        lifecycle_state=LIFECYCLE_DONE,
        summary_template="Web Dev completed implementation changes on branch {branch}.",
        summary_facts={"branch": branch_name or "unspecified"},
        round=step_round,
        conditional=revision_mode,
    )

    return {
        "changes_made": changed_files,
        "implementation_summary": impl_summary,
        "agentic_success": result.success,
    }


async def run_tests(state: dict) -> dict:
    """Run project tests and evaluate results."""
    log = _logger(state)
    log.node("run_tests")
    runtime = state.get("_runtime")
    test_cycles = state.get("test_cycles", 0) + 1
    build_cycles = state.get("build_cycles", 0)
    rebuild_mode = bool(state.get("fix_gaps_attempted"))
    step_key = "wd.rebuilding" if rebuild_mode else "wd.building"
    title = "Web Dev rebuilding and retesting" if rebuild_mode else "Web Dev building and testing"
    step_round = max(test_cycles - 1, 0)
    _record_timeline_step(
        state,
        step_key=step_key,
        title=title,
        summary_template="Web Dev is running the deterministic build and test validation.",
        round=step_round,
        conditional=rebuild_mode,
    )
    max_test_cycles = state.get("max_test_cycles") or int(
        os.environ.get("WEB_DEV_MAX_TEST_CYCLES", "3")
    )
    max_build_cycles = int(os.environ.get("WEB_DEV_MAX_BUILD_RETRIES", "3"))
    log.info("run_tests started", cycle=test_cycles, max_cycles=max_test_cycles)

    if not runtime:
        log.info("run_tests skipped — no runtime (test mode)")
        _record_timeline_step(
            state,
            step_key=step_key,
            title=title,
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Web Dev completed build and test validation in test mode.",
            round=step_round,
            conditional=rebuild_mode,
        )
        return {
            "test_results": {"passed": 1, "failed": 0, "output": ""},
            "test_cycles": test_cycles,
            "test_status": "pass",
            "route": "pass",
        }

    repo_path = state.get("repo_path", "")
    workspace_path = state.get("workspace_path", "")
    if not repo_path or not os.path.isdir(repo_path):
        raise RuntimeError("Mandatory validation cannot run because repo_path is missing")

    log.debug("run_tests running build+test", repo_path=repo_path)
    print(f"[{_AGENT_ID}] run_tests: cycle={test_cycles}/{max_test_cycles} repo_path={repo_path!r}")

    data = _run_mandatory_validation(repo_path, workspace_path, test_cycles)
    failed = data.get("failed", 0)
    install_ok = data.get("install_ok", True)
    build_ok = data.get("build_ok", False)
    test_ok = data.get("test_ok", False)

    # Track build failures separately
    if not build_ok:
        build_cycles += 1
        if build_cycles >= max_build_cycles:
            raise RuntimeError(
                f"Build failed after {build_cycles} retries; cannot proceed."
            )

    test_passed = int(failed) == 0 and install_ok and build_ok and test_ok
    command_summaries = _summarize_validation_commands(data)
    log.info("run_tests result", passed=data.get("passed", 0), failed=failed,
             install_ok=install_ok, build_ok=build_ok, test_ok=test_ok,
             test_passed=test_passed, cycle=test_cycles)
    if command_summaries:
        log.info("run_tests commands", commands=command_summaries)
    if data.get("errors"):
        log.warn("run_tests errors", errors=data.get("errors", []), output_tail=_tail_text(data.get("output", ""), 1200))
    else:
        log.debug("run_tests output tail", output_tail=_tail_text(data.get("output", ""), 500))

    # Write per-cycle test results for auditability
    if workspace_path:
        import time as _time
        results_dir = os.path.join(workspace_path, _AGENT_ID, "test-results")
        os.makedirs(results_dir, exist_ok=True)
        try:
            cycle_file = os.path.join(results_dir, f"test-run-{test_cycles}.json")
            with open(cycle_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "web-dev",
                        "step": "run_tests",
                        "cycle": test_cycles,
                        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": data,
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    if test_passed:
        _record_timeline_step(
            state,
            step_key=step_key,
            title=title,
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Web Dev completed build/test validation with {failed} failing checks.",
            summary_facts={"failed": int(failed)},
            round=step_round,
            conditional=rebuild_mode,
        )
        return {
            "test_results": data,
            "test_output": data.get("output", ""),
            "test_cycles": test_cycles,
            "build_cycles": build_cycles,
            "test_status": "pass",
            "route": "pass",
        }

    if test_cycles >= max_test_cycles:
        print(f"[{_AGENT_ID}] run_tests: max cycles reached ({test_cycles}/{max_test_cycles}); failing task")
        _record_timeline_step(
            state,
            step_key=step_key,
            title=title,
            lifecycle_state=LIFECYCLE_FAILED,
            summary_template="Web Dev exhausted build/test retries after {test_cycles} cycles.",
            summary_facts={"test_cycles": test_cycles},
            round=step_round,
            conditional=rebuild_mode,
        )
        raise RuntimeError(
            "Mandatory validation failed after max cycles; Web Dev cannot proceed to self-assessment or PR"
        )

    return {
        "test_results": data,
        "test_output": data.get("output", ""),
        "test_cycles": test_cycles,
        "build_cycles": build_cycles,
        "test_status": "fail",
        "route": "fail",
    }


async def fix_tests(state: dict) -> dict:
    """Fix failing tests based on test output."""
    log = _logger(state)
    log.node("fix_tests")
    runtime = state.get("_runtime")

    if not runtime:
        log.info("fix_tests skipped — no runtime")
        return {"fix_attempted": True}

    from agents.web_dev.prompts import FIX_SYSTEM, FIX_TEMPLATE

    changed_files = _workflow_changed_files(state)
    prompt = FIX_TEMPLATE.format(
        test_output=state.get("test_output", "No test output available."),
        repo_path=state.get("repo_path", ""),
        changed_files="\n".join(changed_files) if changed_files else "unknown",
    )

    fix_policy, fix_policy_kwargs = _agentic_policy_for_state(state, runtime)
    changed_before = _repo_changed_file_set(state.get("repo_path", ""), state)
    fix_max_turns = _state_env_int(
        state,
        "fix_tests_max_turns",
        "fixTestsMaxTurns",
        "WEB_DEV_FIX_TESTS_MAX_TURNS",
        35,
        minimum=10,
        maximum=200,
    )
    fix_timeout = _state_env_int(
        state,
        "fix_tests_timeout_seconds",
        "fixTestsTimeoutSeconds",
        "WEB_DEV_FIX_TESTS_TIMEOUT_SECONDS",
        900,
        minimum=120,
        maximum=3600,
    )
    result = runtime.run_agentic(
        task=prompt,
        system_prompt=FIX_SYSTEM,
        cwd=_agentic_cwd(runtime, state.get("repo_path") or None),
        max_turns=fix_max_turns,
        timeout=fix_timeout,
        plugin_manager=state.get("_plugin_manager"),
        **fix_policy_kwargs,
    )
    _record_agentic_step_gate(state, step="fix_tests", policy=fix_policy, result=result)
    changed_after = _repo_changed_file_set(state.get("repo_path", ""), state)
    partial_progress = _agentic_partial_progress_detected(
        result,
        changed_before=changed_before,
        changed_after=changed_after,
    )
    fix_summary = _agentic_repair_summary(result, "test-fix") if partial_progress else result.summary
    agentic_success = bool(result.success or partial_progress)
    if partial_progress:
        log.info("fix_tests partial progress accepted", summary=fix_summary[:300])

    # Validation gate: ensure fix actually changed files
    from framework.validation_gates import validate_files_changed
    gate_result = validate_files_changed(state.get("repo_path", ""))
    if not gate_result.passed and "No file changes detected" in gate_result.feedback:
        log.warn("fix_tests produced no file changes", feedback=gate_result.feedback)

    return {
        "fix_attempted": True,
        "fix_summary": fix_summary,
        "agentic_success": agentic_success,
        "changes_made": _workflow_changed_files(state),
        "test_cycles": 0,
        "build_cycles": 0,
    }


def _self_assess_agentic_fallback_enabled() -> bool:
    """Return whether the agentic-file fallback path for self_assess is enabled.

    Operators can disable the fallback with
    ``WEB_DEV_SELF_ASSESS_AGENTIC_FALLBACK=0`` if they need to debug the
    text-mode parser in isolation. Enabled by default.
    """
    raw = os.environ.get("WEB_DEV_SELF_ASSESS_AGENTIC_FALLBACK", "1").strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def _self_assess_via_agentic_file(
    *,
    state: dict,
    runtime: Any,
    base_prompt: str,
    system_prompt: str,
    cwd: str | None,
    workspace_path: str,
    agent_dir_name: str,
    cycle: int,
    plugin_manager: Any,
) -> tuple[dict[str, Any] | None, str]:
    """Fallback path: ask the agentic CLI to write the self-assessment to a file.

    The text-mode :func:`runtime.run` path can return prose, ``<think>`` blocks,
    or truncated JSON that no parser can recover. Every supported agentic
    backend (``copilot-cli``, ``claude-code``, ``codex-cli``) does support
    :func:`runtime.run_agentic` with a *cwd*, so we drive the CLI to write
    its JSON answer to a deterministic file path under the per-task
    workspace. We then read the file back as the source of truth.

    Returns ``(parsed_dict_or_None, file_path)``. Caller is responsible for
    feeding ``parsed_dict_or_None`` into ``validate_self_assessment``.
    """
    if not workspace_path:
        return None, ""
    if not hasattr(runtime, "run_agentic"):
        return None, ""

    agent_dir = os.path.join(workspace_path, agent_dir_name)
    try:
        os.makedirs(agent_dir, exist_ok=True)
    except OSError:
        return None, ""

    output_path = os.path.join(agent_dir, f"self-assessment-llm-{cycle}.json")
    # Best-effort: drop any stale copy from a previous attempt so the
    # presence of the file after the agent finishes is a real signal.
    try:
        if os.path.isfile(output_path):
            os.unlink(output_path)
    except OSError:
        pass

    fallback_instructions = (
        "The previous direct response could not be parsed as valid JSON. "
        "Use your file-writing tool to write the self-assessment JSON to the "
        f"absolute path below. Do NOT write any other files; do NOT modify "
        "any source code; do NOT run git or shell commands beyond writing "
        "this single file.\n\n"
        f"Target file: {output_path}\n\n"
        "The file must contain exactly ONE top-level JSON object with these "
        "keys (no markdown fences, no <think> blocks, no surrounding prose): "
        "score (float 0.0-1.0), verdict ('pass' or 'fail'), criteria_checks "
        "(array), component_checks (array), self_review_issues (array), "
        "gaps (array), summary (string).\n\n"
        "After writing the file, print only the absolute file path on a "
        "single line.\n\n"
        "--- Original self-assessment task ---\n"
        f"{base_prompt}"
    )

    policy, policy_kwargs = _agentic_policy_for_state(state, runtime)
    try:
        agentic_result = runtime.run_agentic(
            task=fallback_instructions,
            system_prompt=system_prompt,
            cwd=_agentic_cwd(runtime, cwd or workspace_path or None),
            max_turns=4,
            timeout=int(os.environ.get("WEB_DEV_SELF_ASSESS_AGENTIC_TIMEOUT", "240") or "240"),
            plugin_manager=plugin_manager,
            **policy_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        return None, output_path
    _record_agentic_step_gate(
        state,
        step="self_assess_agentic_file",
        policy=policy,
        result=agentic_result,
    )

    # Even when the CLI returns success=False we still try to read the file
    # — some backends report non-zero exit but did write the artifact first.
    if not os.path.isfile(output_path):
        return None, output_path
    try:
        with open(output_path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return None, output_path

    from framework.json_extract import extract_json_object

    parsed = extract_json_object(
        raw,
        required_keys={"score", "verdict"},
    )
    if parsed is None:
        # The file exists but the content is still malformed — try the
        # generic extractor as a last-ditch parse.
        parsed = extract_json_object(raw)

    return parsed, output_path


def _is_implementation_ground_truth_present(state: dict) -> bool:
    """Return True when observable evidence shows the implementation is in place.

    This is a generic, methodology-level check — it uses only signals the
    workflow has already collected (test_results, changes_made) without
    consulting any task-specific file paths or component names. It exists
    to detect the failure mode where the model reports a fail verdict in
    its self-assessment even though the build, tests, and changes
    contradict that verdict (an LLM hallucination).
    """
    test_results = state.get("test_results", {}) or {}
    if not isinstance(test_results, dict):
        return False
    if not test_results.get("build_ok"):
        return False
    if not test_results.get("test_ok"):
        return False
    if int(test_results.get("passed", 0) or 0) <= 0:
        return False
    changes_made = _workflow_changed_files(state)
    if not changes_made:
        return False
    return True


def _self_assessment_claims_conflict_with_ground_truth(
    state: dict, data: dict
) -> tuple[bool, str]:
    """Detect when the model's self-assessment claims contradict observable reality.

    The agent may write ``self_review_issues`` like ``"<some-file> does
    not exist"`` even though the file is on disk. Such claims are
    strong evidence of a hallucination. We do not need to know anything
    about the project to spot the contradiction — we only check the
    language of the claim against the filesystem.

    Returns ``(conflict, reason)``. ``conflict`` is True when at least one
    self-review issue says a file is missing / not implemented / not
    created / not modified AND the file actually exists on disk with
    non-trivial content.
    """
    repo_path = state.get("repo_path", "") or ""
    if not repo_path:
        return False, ""
    issues = data.get("self_review_issues", [])
    if not isinstance(issues, list):
        return False, ""

    hallucination_phrases = (
        "cannot verify",
        "could not verify",
        "unable to verify",
        "not inspectable",
        "not available for inspection",
        "not available for review",
        "does not exist",
        "is missing",
        "not implemented",
        "has not been implemented",
        "not created",
        "no new ",
        "is not present",
        "not modified",
        "changed files list does not include",
        "not included in the changed files",
        "not in changed files",
    )
    contradicted: list[str] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        file_ref = str(issue.get("file") or "").strip()
        if not file_ref:
            continue
        message = str(issue.get("message", "")).lower()
        if not any(phrase in message for phrase in hallucination_phrases):
            continue
        full_path = os.path.join(repo_path, file_ref)
        try:
            if os.path.isfile(full_path) and os.path.getsize(full_path) > 50:
                contradicted.append(file_ref)
        except OSError:
            continue
    if contradicted:
        return True, (
            "self_review_issues claim these files are missing/not implemented, "
            f"but they exist on disk: {contradicted[:5]}"
        )
    return False, ""


def _build_ground_truth_re_prompt(
    state: dict, data: dict, original_prompt: str
) -> str:
    """Construct a re-prompt that surfaces observable ground truth to the model.

    The re-prompt never references any task-specific component name, file
    path, or acceptance criterion. It just states what the workflow can
    already observe and asks the model to re-evaluate.
    """
    test_results = state.get("test_results", {}) or {}
    changes_made = _workflow_changed_files(state)
    source_evidence = _build_self_assessment_source_evidence(
        state.get("repo_path", "") or "",
        changes_made,
    )
    files_sample = ", ".join(str(f) for f in changes_made[:8])
    if len(changes_made) > 8:
        files_sample += f" (and {len(changes_made) - 8} more)"

    previous_issues = data.get("self_review_issues", []) or []
    previous_blocking = [
        issue for issue in previous_issues
        if isinstance(issue, dict) and issue.get("blocking", True) is not False
    ]
    previous_gaps = data.get("gaps", []) or []

    re_prompt = f"""Your previous self-assessment reported gaps, but the workflow's
observable ground truth contradicts that report. Please re-evaluate the
implementation against the actual file contents (read the files first)
and produce a new self-assessment.

GROUND TRUTH (these are facts, not estimates):
- Build: {'passed' if test_results.get('build_ok') else 'failed'}
- Tests: {int(test_results.get('passed', 0) or 0)} passed, {int(test_results.get('failed', 0) or 0)} failed
- Files changed or created in the implementation step ({len(changes_made)}): {files_sample or '(none)'}

DETERMINISTIC SOURCE EVIDENCE:
{source_evidence}

YOUR PREVIOUS REPORT:
- verdict={data.get('verdict')!r}, score={data.get('score')!r}
- {len(previous_blocking)} blocking self-review issue(s)
- {len(previous_gaps)} gap(s) reported

If the build and tests passed and the files listed above exist with content,
your previous assessment is very likely a hallucination. Read the actual
file contents before re-assessing. Do NOT claim a file is missing or
"not implemented" without first reading it.

Return the same JSON schema as before. If the implementation is actually
complete, set verdict='pass' and self_review_issues=[] (or include only
genuine blocking issues you confirmed by reading the files).

--- Original self-assessment task (re-evaluate against ground truth) ---
{original_prompt}
"""
    return re_prompt


def _try_ground_truth_re_prompt(
    *,
    state: dict,
    data: dict,
    runtime: Any,
    prompt: str,
    system_prompt: str,
    acceptance_criteria_count: int,
    log: Any,
) -> tuple[bool, dict[str, Any] | None, str]:
    """Run a single re-prompt with ground truth context.

    Used as the last line of defence against self-assessment hallucinations
    when the model hits ``max_assess_cycles`` with a fail verdict that
    contradicts observable reality.

    Returns ``(recovered, new_data, reason)``. When ``recovered`` is True,
    ``new_data`` is a validated self-assessment that the caller may use in
    place of the hallucinated one. When ``recovered`` is False, ``reason``
    explains why the re-prompt did not help.
    """
    re_prompt = _build_ground_truth_re_prompt(state, data, prompt)
    try:
        result = runtime.run(
            re_prompt,
            system_prompt=system_prompt,
            max_tokens=4096,
            plugin_manager=state.get("_plugin_manager"),
            cwd=state.get("repo_path") or None,
        )
    except Exception as exc:  # noqa: BLE001
        return False, None, f"ground-truth re-prompt raised: {exc!r}"

    raw_response = result.get("raw_response", "") if isinstance(result, dict) else ""
    parsed = _safe_json(raw_response, fallback={})
    if not isinstance(parsed, dict) or not parsed:
        return False, None, "ground-truth re-prompt produced no JSON"

    candidate = dict(parsed)
    candidate.setdefault("criteria_checks", [])
    candidate.setdefault("component_checks", [])
    candidate.setdefault("self_review_issues", [])
    candidate.setdefault("gaps", [])
    candidate.setdefault("summary", "")

    from framework.validation_gates import validate_self_assessment

    gate_result = validate_self_assessment(candidate, acceptance_criteria_count)
    if not gate_result.passed:
        return False, None, f"ground-truth re-prompt failed validation: {gate_result.feedback}"

    # Even after the re-prompt, double-check the model didn't re-hallucinate
    # by claiming the same contradictory files. If it did, treat the
    # re-prompt as a no-op so we still surface the failure to the user.
    conflict, conflict_reason = _self_assessment_claims_conflict_with_ground_truth(
        state, candidate
    )
    if conflict:
        return False, None, f"ground-truth re-prompt still conflicts: {conflict_reason}"

    return True, candidate, ""


def _apply_deterministic_gaps(
    state: dict, data: dict, schema_failure_exhausted: bool
) -> None:
    """Mutate ``data`` in place: surface self-review issues as gaps and
    downgrade the verdict when blocking issues exist.

    Pulled out of :func:`self_assess` so the ground-truth re-prompt
    recovery path can re-apply the same transformation to a corrected
    self-assessment.
    """
    deterministic_gaps: list[str] = []
    blocking_issue_gaps: list[str] = []
    deterministic_findings = data.get("deterministic_findings")
    if not isinstance(deterministic_findings, list):
        deterministic_findings = []
        data["deterministic_findings"] = deterministic_findings
    self_review_issues = data.get("self_review_issues", [])
    if not isinstance(self_review_issues, list):
        self_review_issues = []
        data["self_review_issues"] = self_review_issues
    if self_review_issues:
        for issue in self_review_issues:
            if not isinstance(issue, dict):
                continue
            message = str(issue.get("message", "")).strip()
            if not message:
                continue
            file_ref = str(issue.get("file") or "").strip()
            line_ref = issue.get("line")
            location = file_ref
            if file_ref and line_ref:
                location = f"{file_ref}:{line_ref}"
            gap = f"{location} - {message}" if location else message
            # An issue is "blocking" when the agent set ``blocking=True`` or
            # left the field unset (legacy default). Non-blocking issues are
            # advisory and must not downgrade the verdict on their own.
            is_blocking = issue.get("blocking", True) is not False
            if is_blocking:
                blocking_issue_gaps.append(gap)
            deterministic_gaps.append(gap)

    if _is_screenshot_required(state) and not schema_failure_exhausted:
        icon_validation = _detect_fragile_icon_font_usage(state.get("repo_path", ""))
        deterministic_gaps.extend(icon_validation.get("issues") or [])
        # Icon-rendering gaps are deterministic, file-level findings; treat
        # them as blocking so they keep their existing fail semantics.
        blocking_issue_gaps.extend(icon_validation.get("issues") or [])
        if icon_validation.get("issues"):
            deterministic_findings.append(
                {
                    "kind": "fragile_icon_font_usage",
                    "blocking": True,
                    "files": icon_validation.get("files") or [],
                    "icon_tokens": icon_validation.get("icon_tokens") or [],
                    "message": icon_validation["issues"][0],
                    "recommended_fix": (
                        "Replace icon-font ligature text and Material icon "
                        "classes in runtime markup with inline SVG or local "
                        "React icon components."
                    ),
                }
            )
            data["component_checks"].append(
                {
                    "component": "Icon rendering",
                    "status": "incomplete",
                    "notes": icon_validation["issues"][0],
                }
            )

    if deterministic_gaps and not schema_failure_exhausted:
        merged_gaps: list[str] = []
        for gap in [*(data.get("gaps") or []), *deterministic_gaps]:
            text = str(gap).strip()
            if text and text not in merged_gaps:
                merged_gaps.append(text)
        data["gaps"] = merged_gaps
        # Only BLOCKING issues downgrade the verdict. Advisory / non-blocking
        # issues are surfaced in the gap list for visibility but the
        # implementation is allowed to pass when they are the only findings.
        if blocking_issue_gaps:
            data["verdict"] = "fail"
            data["score"] = min(float(data.get("score", 0) or 0), 0.89)
            summary = str(data.get("summary", "")).strip()
            if blocking_issue_gaps[0] not in summary:
                data["summary"] = (summary + " " if summary else "") + blocking_issue_gaps[0]


async def self_assess(state: dict) -> dict:
    """Run requirement-aware and design-aware self assessment."""
    log = _logger(state)
    log.node("self_assess")
    runtime = state.get("_runtime")
    assess_cycles = state.get("assess_cycles", 0) + 1
    retry_mode = bool(state.get("fix_gaps_attempted") or assess_cycles > 1)
    step_key = "wd.self_check_retry" if retry_mode else "wd.self_check"
    title = "Web Dev rerunning self-check" if retry_mode else "Web Dev running self-check"
    step_round = max(assess_cycles - 2, 0) if retry_mode else 0
    max_assess_cycles = _state_env_int(
        state,
        "max_assess_cycles",
        "maxAssessCycles",
        "WEB_DEV_MAX_ASSESS_CYCLES",
        3,
        minimum=1,
        maximum=10,
    )
    log.info("self_assess started", cycle=assess_cycles, max_cycles=max_assess_cycles)
    _record_timeline_step(
        state,
        step_key=step_key,
        title=title,
        summary_template="Web Dev is verifying the implementation against requirements.",
        round=step_round,
        conditional=retry_mode,
    )

    if not runtime:
        _record_timeline_step(
            state,
            step_key=step_key,
            title=title,
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Web Dev completed the self-check in test mode.",
            round=step_round,
            conditional=retry_mode,
        )
        return {
            "self_assessment": {
                "score": 0.95,
                "verdict": "pass",
                "gaps": [],
                "component_checks": [],
                "criteria_checks": [],
            },
            "assess_cycles": assess_cycles,
            "route": "pass",
        }

    from agents.web_dev.prompts import SELF_ASSESS_SYSTEM, SELF_ASSESS_TEMPLATE

    jira_ctx = state.get("jira_context", {})
    design_ctx = state.get("design_context") or {}
    workspace_path = state.get("workspace_path", "")

    # Try to load full design context from workspace file (more complete than state copy)
    if workspace_path:
        design_spec_path = os.path.join(workspace_path, "team-lead", "design-spec.json")
        if os.path.isfile(design_spec_path):
            try:
                with open(design_spec_path, encoding="utf-8") as _f:
                    spec_data = json.load(_f)
                design_ctx = spec_data.get("data", design_ctx) or design_ctx
            except Exception:
                pass

    # Load design HTML code for component-by-component comparison
    design_code_snippet = ""
    design_code_path = state.get("design_code_path", "")
    # Prefer ui-design/stitch/code.html, fallback to team-lead/design-code.html
    if not design_code_path and workspace_path:
        _stitch_code = os.path.join(workspace_path, "ui-design", "stitch", "code.html")
        _legacy_code = os.path.join(workspace_path, "team-lead", "design-code.html")
        design_code_path = _stitch_code if os.path.isfile(_stitch_code) else _legacy_code
    if design_code_path and os.path.isfile(design_code_path):
        try:
            with open(design_code_path, encoding="utf-8") as _f:
                design_html = _f.read()
            design_code_snippet = text_for_prompt(
                design_html,
                max_chars=int(os.environ.get("WEB_DEV_SELF_ASSESS_DESIGN_HTML_MAX_CHARS", "12000") or "12000"),
                default="",
            )
        except Exception:
            pass

    # Load design spec markdown (typography/colors/spacing) for component comparison
    design_spec_markdown = ""
    if workspace_path:
        _stitch_md = os.path.join(workspace_path, "ui-design", "stitch", "DESIGN.md")
        _legacy_md = os.path.join(workspace_path, "team-lead", "design-spec.md")
        design_spec_md_path = state.get("design_md_path", "") or (
            _stitch_md if os.path.isfile(_stitch_md) else _legacy_md
        )
        if os.path.isfile(design_spec_md_path):
            try:
                with open(design_spec_md_path, encoding="utf-8") as _f:
                    design_spec_markdown = text_for_prompt(
                        _f.read(),
                        max_chars=int(os.environ.get("WEB_DEV_SELF_ASSESS_DESIGN_SPEC_MAX_CHARS", "8000") or "8000"),
                        default="",
                    )
            except Exception:
                pass

    acceptance_criteria = []
    if isinstance(jira_ctx, dict):
        fields = jira_ctx.get("fields", jira_ctx)
        acceptance_criteria = fields.get("acceptanceCriteria", [])
        if not acceptance_criteria and fields.get("description"):
            desc = fields["description"]
            if isinstance(desc, dict):
                desc = json.dumps(desc, ensure_ascii=False)[:1500]
            elif isinstance(desc, str):
                desc = desc[:1500]
            acceptance_criteria = [desc]
    # Truncate criteria list to avoid context overflow
    ac_str = json.dumps(acceptance_criteria[:5], ensure_ascii=False)
    if len(ac_str) > 3000:
        ac_str = ac_str[:3000] + "...]"
    acceptance_criteria_count = len(acceptance_criteria) if isinstance(acceptance_criteria, list) else 0

    changed_files_list = _workflow_changed_files(state)
    source_evidence = _build_self_assessment_source_evidence(
        state.get("repo_path", "") or "",
        changed_files_list,
    )

    # Load Code Review comments for revision mode self-assessment
    cr_comments_text = ""
    revision_feedback = state.get("revision_feedback", "")
    review_report_path = state.get("review_report_path", "")
    if revision_feedback and workspace_path:
        # Try to load structured review report for detailed issue checking
        if review_report_path:
            report_full_path = review_report_path
            if not os.path.isabs(report_full_path):
                report_full_path = os.path.join(workspace_path, report_full_path)
            if os.path.isfile(report_full_path):
                try:
                    with open(report_full_path, encoding="utf-8") as _f:
                        review_data = json.load(_f)
                    report_body = review_data.get("data", review_data)
                    comments = report_body.get("all_comments", []) or report_body.get("comments", [])
                    cr_lines = []
                    for c in comments[:20]:
                        if not _review_comment_requires_fix(c):
                            continue
                        sev = c.get("severity", "info")
                        msg = c.get("message", "")
                        file_ref = c.get("file", "")
                        cr_lines.append(f"- [{sev}] {file_ref}: {msg}" if file_ref else f"- [{sev}] {msg}")
                    cr_comments_text = "\n".join(cr_lines)
                except Exception:
                    pass
        if not cr_comments_text and revision_feedback:
            cr_comments_text = revision_feedback

    prompt = SELF_ASSESS_TEMPLATE.format(
        acceptance_criteria=ac_str,
        design_context=text_for_prompt(design_ctx, max_chars=800, default="N/A (not a UI task)"),
        design_code_snippet=design_code_snippet or "N/A (no design HTML available)",
        design_spec_markdown=design_spec_markdown or "N/A (no design spec available)",
        implementation_summary=str(state.get("implementation_summary", ""))[:1000],
        test_results=json.dumps(state.get("test_results", {}), ensure_ascii=False)[:500],
        changed_files="\n".join(changed_files_list) or "unknown",
        source_evidence=source_evidence,
        review_issue_schema=REVIEW_ISSUE_SCHEMA,
    )

    # Inject coding standards for self-assessment evaluation
    try:
        from framework.standards_loader import (
            detect_tech_stack_from_repo,
            format_standards_for_prompt,
            load_standards,
        )
        ts = state.get("tech_stack") or []
        if not ts and state.get("repo_path"):
            ts = detect_tech_stack_from_repo(state["repo_path"])
        std_rules = load_standards(tech_stack=ts, agent_role="development")
        std_text = format_standards_for_prompt(std_rules, agent_role="development")
        if std_text:
            prompt += f"\n\n{std_text}\n"
    except Exception:
        pass

    # Append CR comments section for revision-mode assessment
    if cr_comments_text:
        prompt += f"""

## Merge-Blocking Code Review Issues (MUST verify all are fixed):

{cr_comments_text}

IMPORTANT: The list above already excludes advisory comments.
For each merge-blocking code review issue listed above, verify the fix is
present in the changed files. Add a "cr_issues_fixed" field to your response
indicating whether ALL merge-blocking CR issues are resolved (true/false).
If any merge-blocking issue is NOT fixed, set verdict to "fail" and list the
unresolved issues in gaps.
"""

    from framework.validation_gates import validate_self_assessment

    gate_result = None
    data: dict[str, Any] = {}
    schema_failure_exhausted = False
    try:
        max_schema_attempts = int(os.environ.get("WEB_DEV_SELF_ASSESS_SCHEMA_ATTEMPTS", "4") or "4")
    except ValueError:
        max_schema_attempts = 4
    max_schema_attempts = max(1, min(max_schema_attempts, 8))
    for schema_attempt in range(1, max_schema_attempts + 1):
        attempt_prompt = prompt
        if gate_result and gate_result.feedback:
            attempt_prompt += f"""

IMPORTANT: Your previous self-assessment response was invalid.
Return valid JSON only, and correct this validation feedback:
{gate_result.feedback}
"""

        result = runtime.run(
            attempt_prompt,
            system_prompt=SELF_ASSESS_SYSTEM,
            max_tokens=4096,
            plugin_manager=state.get("_plugin_manager"),
            cwd=state.get("repo_path") or None,
        )

        raw_response = result.get("raw_response", "")
        print(f"[{_AGENT_ID}] self_assess raw_response (first 500 chars): {raw_response[:500]!r}")
        parsed = _safe_json(raw_response, fallback={})
        if not parsed:
            print(f"[{_AGENT_ID}] self_assess _safe_json returned empty — raw_response type={type(raw_response).__name__}, len={len(raw_response) if raw_response else 0}")

        data = parsed if isinstance(parsed, dict) else {}
        data.setdefault("criteria_checks", [])
        data.setdefault("component_checks", [])
        data.setdefault("self_review_issues", [])
        data.setdefault("gaps", [])
        data.setdefault("summary", "")

        gate_result = validate_self_assessment(data, acceptance_criteria_count)
        if gate_result.passed:
            break
        log.warn("validate_self_assessment gate", feedback=gate_result.feedback, schema_attempt=schema_attempt)
    else:
        # Text-mode retries exhausted. Before escalating to the user, give
        # the agentic CLI a chance to write the JSON to a file directly —
        # this bypasses the brittle text-LLM return path entirely and works
        # uniformly across copilot-cli, claude-code, and codex-cli backends.
        text_mode_feedback = gate_result.feedback if gate_result else "Self-assessment output was invalid."
        fallback_used = False
        fallback_used_with_feedback = False
        fallback_feedback = ""
        fallback_file_path = ""
        if _self_assess_agentic_fallback_enabled():
            log.info(
                "self_assess attempting agentic-file fallback",
                cycle=assess_cycles,
                text_mode_feedback=text_mode_feedback[:200],
            )
            fallback_parsed, fallback_file_path = _self_assess_via_agentic_file(
                state=state,
                runtime=runtime,
                base_prompt=prompt,
                system_prompt=SELF_ASSESS_SYSTEM,
                cwd=state.get("repo_path") or None,
                workspace_path=state.get("workspace_path", ""),
                agent_dir_name=_AGENT_ID,
                cycle=assess_cycles,
                plugin_manager=state.get("_plugin_manager"),
            )
            if isinstance(fallback_parsed, dict):
                candidate = dict(fallback_parsed)
                candidate.setdefault("criteria_checks", [])
                candidate.setdefault("component_checks", [])
                candidate.setdefault("self_review_issues", [])
                candidate.setdefault("gaps", [])
                candidate.setdefault("summary", "")
                fallback_gate = validate_self_assessment(candidate, acceptance_criteria_count)
                if fallback_gate.passed:
                    log.info(
                        "self_assess agentic-file fallback succeeded",
                        cycle=assess_cycles,
                        file=fallback_file_path,
                    )
                    data = candidate
                    gate_result = fallback_gate
                    fallback_used = True
                else:
                    fallback_feedback = fallback_gate.feedback
                    fallback_used_with_feedback = True
                    log.warn(
                        "self_assess agentic-file fallback failed validation",
                        cycle=assess_cycles,
                        feedback=fallback_feedback,
                        file=fallback_file_path,
                    )
            else:
                fallback_used_with_feedback = True
                fallback_feedback = "Agentic-file fallback produced no usable JSON."
                log.warn(
                    "self_assess agentic-file fallback produced no usable JSON",
                    cycle=assess_cycles,
                    file=fallback_file_path,
                )

        if not fallback_used:
            schema_failure_exhausted = True
            # The user-facing error message must reflect the *most recent*
            # failure. If the agentic-file fallback also failed, its feedback
            # is what the agent actually said — surface that, not the older
            # text-mode feedback which describes an earlier parser failure.
            surface_feedback = (
                fallback_feedback if fallback_used_with_feedback else text_mode_feedback
            )
            fallback_attempted = (
                _self_assess_agentic_fallback_enabled() and bool(fallback_file_path)
            )
            data = {
                "score": 0.0,
                "verdict": "error",
                "criteria_checks": data.get("criteria_checks", []) if isinstance(data, dict) else [],
                "component_checks": data.get("component_checks", []) if isinstance(data, dict) else [],
                "self_review_issues": [],
                "gaps": [
                    f"Self-assessment output invalid after {max_schema_attempts} schema attempt(s)"
                    + (" and agentic-file fallback" if fallback_attempted else "")
                    + f": {surface_feedback}"
                ],
                "summary": (
                    f"Self-assessment output invalid after {max_schema_attempts} schema attempt(s)"
                    + (" and agentic-file fallback" if fallback_attempted else "")
                    + f": {surface_feedback}"
                ),
                "failure_type": "schema",
                "schema_feedback": surface_feedback,
                "fallback_attempted": fallback_attempted,
                "fallback_file": fallback_file_path,
            }

    _apply_deterministic_gaps(state, data, schema_failure_exhausted)

    score = float(data.get("score", 0) or 0)
    verdict = data.get("verdict", "fail")
    gaps = data.get("gaps", []) or []

    print(f"[{_AGENT_ID}] self_assess result: score={score} verdict={verdict} gaps={len(gaps)}")

    # Write self-assessment.json to workspace
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        import time as _time
        agent_dir = os.path.join(workspace_path, _AGENT_ID)
        os.makedirs(agent_dir, exist_ok=True)
        try:
            sa_file = os.path.join(agent_dir, f"self-assessment-{assess_cycles}.json")
            with open(sa_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "web-dev",
                        "step": "self_assess",
                        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "version": assess_cycles,
                    },
                    "data": data,
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    if score >= 0.9 and verdict != "fail":
        route = "pass"
    elif schema_failure_exhausted:
        route = "need_user_input"
    elif assess_cycles >= max_assess_cycles and runtime is not None:
        # Last line of defence: before declaring failure, check whether the
        # self-assessment contains a machine-verifiable contradiction, such
        # as claiming a changed file does not exist when it is present on
        # disk. Passing build/test output alone is not enough to override a
        # failed self-check, because that lets a backend revise a genuine
        # 0.85-style failure into a pass without doing more work.
        claims_conflict, conflict_reason = _self_assessment_claims_conflict_with_ground_truth(
            state, data
        )
        ground_truth_present = _is_implementation_ground_truth_present(state)
        if ground_truth_present and claims_conflict:
            log.warn(
                "self_assess attempting ground-truth re-prompt",
                cycle=assess_cycles,
                reason=conflict_reason,
            )
            recovered, gt_data, gt_reason = _try_ground_truth_re_prompt(
                state=state,
                data=data,
                runtime=runtime,
                prompt=prompt,
                system_prompt=SELF_ASSESS_SYSTEM,
                acceptance_criteria_count=acceptance_criteria_count,
                log=log,
            )
            if recovered and isinstance(gt_data, dict):
                # Adopt the re-prompted assessment. Re-apply the
                # deterministic-gaps/verdict-downgrade step so advisory
                # issues get surfaced and blocking issues still force a
                # fail verdict.
                original_score = score
                original_verdict = verdict
                data = gt_data
                _apply_deterministic_gaps(state, data, schema_failure_exhausted)
                data["ground_truth_reprompt"] = {
                    "applied": True,
                    "original_score": original_score,
                    "original_verdict": original_verdict,
                    "reason": conflict_reason,
                }
                score = float(data.get("score", 0) or 0)
                verdict = data.get("verdict", "fail")
                gaps = data.get("gaps", []) or []
                log.info(
                    "self_assess ground-truth re-prompt recovered",
                    cycle=assess_cycles,
                    score=score,
                    verdict=verdict,
                    gaps=len(gaps),
                )
                if score >= 0.9 and verdict != "fail":
                    route = "pass"
                else:
                    failure_summary = "; ".join(str(gap) for gap in gaps[:4]) or str(data.get("summary", "self-assessment failed"))
                    log.warn("self_assess exhausted retries", cycle=assess_cycles, failure_summary=failure_summary[:400])
                    _record_timeline_step(
                        state,
                        step_key=step_key,
                        title=title,
                        lifecycle_state=LIFECYCLE_FAILED,
                        summary_template="Web Dev self-check exhausted retries with verdict {verdict}.",
                        summary_facts={"verdict": verdict},
                        round=step_round,
                        conditional=retry_mode,
                    )
                    raise RuntimeError(f"self_assess failed after {max_assess_cycles} cycles: {failure_summary[:400]}")
                # Persist the corrected assessment to disk.
                workspace_path = state.get("workspace_path", "")
                if workspace_path:
                    import time as _time
                    agent_dir = os.path.join(workspace_path, _AGENT_ID)
                    os.makedirs(agent_dir, exist_ok=True)
                    try:
                        sa_file = os.path.join(agent_dir, f"self-assessment-{assess_cycles}.json")
                        with open(sa_file, "w", encoding="utf-8") as fh:
                            json.dump({
                                "metadata": {
                                    "agent_id": "web-dev",
                                    "step": "self_assess",
                                    "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                    "version": assess_cycles,
                                },
                                "data": data,
                            }, fh, ensure_ascii=False, indent=2)
                    except OSError:
                        pass
                # Skip the original "exhausted retries" path — we recovered.
            else:
                log.warn(
                    "self_assess ground-truth re-prompt did not help",
                    cycle=assess_cycles,
                    reason=gt_reason,
                )
                failure_summary = "; ".join(str(gap) for gap in gaps[:4]) or str(data.get("summary", "self-assessment failed"))
                log.warn("self_assess exhausted retries", cycle=assess_cycles, failure_summary=failure_summary[:400])
                _record_timeline_step(
                    state,
                    step_key=step_key,
                    title=title,
                    lifecycle_state=LIFECYCLE_FAILED,
                    summary_template="Web Dev self-check exhausted retries with verdict {verdict}.",
                    summary_facts={"verdict": verdict},
                    round=step_round,
                    conditional=retry_mode,
                )
                raise RuntimeError(f"self_assess failed after {max_assess_cycles} cycles: {failure_summary[:400]}")
        else:
            failure_summary = "; ".join(str(gap) for gap in gaps[:4]) or str(data.get("summary", "self-assessment failed"))
            log.warn("self_assess exhausted retries", cycle=assess_cycles, failure_summary=failure_summary[:400])
            _record_timeline_step(
                state,
                step_key=step_key,
                title=title,
                lifecycle_state=LIFECYCLE_FAILED,
                summary_template="Web Dev self-check exhausted retries with verdict {verdict}.",
                summary_facts={"verdict": verdict},
                round=step_round,
                conditional=retry_mode,
            )
            raise RuntimeError(f"self_assess failed after {max_assess_cycles} cycles: {failure_summary[:400]}")
    elif assess_cycles >= max_assess_cycles:
        failure_summary = "; ".join(str(gap) for gap in gaps[:4]) or str(data.get("summary", "self-assessment failed"))
        log.warn("self_assess exhausted retries", cycle=assess_cycles, failure_summary=failure_summary[:400])
        _record_timeline_step(
            state,
            step_key=step_key,
            title=title,
            lifecycle_state=LIFECYCLE_FAILED,
            summary_template="Web Dev self-check exhausted retries with verdict {verdict}.",
            summary_facts={"verdict": verdict},
            round=step_round,
            conditional=retry_mode,
        )
        raise RuntimeError(f"self_assess failed after {max_assess_cycles} cycles: {failure_summary[:400]}")
    else:
        route = "fail"

    log.info(
        "self_assess result",
        score=score,
        verdict=verdict,
        gaps=len(gaps),
        route=route,
        cycle=assess_cycles,
    )
    if gaps:
        log.warn("self_assess gaps", gaps=gaps[:10], summary=str(data.get("summary", ""))[:300])

    if route == "pass":
        _record_timeline_step(
            state,
            step_key=step_key,
            title=title,
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Web Dev completed the self-check with verdict {verdict}.",
            summary_facts={"verdict": verdict},
            round=step_round,
            conditional=retry_mode,
        )
        return {
            "self_assessment": data,
            "assess_cycles": assess_cycles,
            "route": "pass",
        }

    # Per design doc §13.1 A5 / §0.5: an intermediate self-check that fails
    # but the task will retry must use the warn visual (LIFECYCLE_WARNING)
    # rather than done or failed. The failed visual is reserved for the
    # final ``compass.task_failed#0`` terminal row.
    _record_timeline_step(
        state,
        step_key=step_key,
        title=title,
        lifecycle_state=LIFECYCLE_WARNING,
        summary_template="Web Dev self-check found remaining gaps with verdict {verdict}.",
        summary_facts={"verdict": verdict, "gap_count": len(gaps)},
        round=step_round,
        conditional=retry_mode,
    )
    return {
        "self_assessment": data,
        "assess_cycles": assess_cycles,
        "route": route,
    }


async def fix_gaps(state: dict) -> dict:
    """Fix self-assessment gaps before re-running tests and self-assessment."""
    log = _logger(state)
    log.node("fix_gaps")
    runtime = state.get("_runtime")
    gap_round = max(int(state.get("assess_cycles", 0) or 0) - 1, 0)
    _record_timeline_step(
        state,
        step_key="wd.fixing_gaps",
        title="Web Dev fixing self-check gaps",
        summary_template="Web Dev is addressing the gaps found during self-check.",
        round=gap_round,
        conditional=True,
    )

    if not runtime:
        log.info("fix_gaps skipped — no runtime")
        _record_timeline_step(
            state,
            step_key="wd.fixing_gaps",
            title="Web Dev fixing self-check gaps",
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Web Dev completed the self-check gap fix in test mode.",
            round=gap_round,
            conditional=True,
        )
        return {"fix_gaps_attempted": True}

    from agents.web_dev.prompts import FIX_GAPS_SYSTEM, FIX_GAPS_TEMPLATE

    assessment = state.get("self_assessment", {})
    gaps = assessment.get("gaps", [])
    deterministic_findings = assessment.get("deterministic_findings", [])
    changed_files = _workflow_changed_files(state)
    log.info("fix_gaps started", gaps=len(gaps), files_changed=len(changed_files))

    prompt = FIX_GAPS_TEMPLATE.format(
        gaps="\n".join(f"- {g}" for g in gaps) if gaps else "No specific gaps listed.",
        deterministic_findings=(
            json.dumps(deterministic_findings, ensure_ascii=False, indent=2)
            if deterministic_findings else "[]"
        ),
        repo_path=state.get("repo_path", ""),
        changed_files="\n".join(changed_files) if changed_files else "unknown",
    )

    fix_gaps_policy, fix_gaps_policy_kwargs = _agentic_policy_for_state(state, runtime)
    changed_before = _repo_changed_file_set(state.get("repo_path", ""), state)
    fix_gaps_max_turns = _state_env_int(
        state,
        "fix_gaps_max_turns",
        "fixGapsMaxTurns",
        "WEB_DEV_FIX_GAPS_MAX_TURNS",
        30,
        minimum=10,
        maximum=200,
    )
    fix_gaps_timeout = _state_env_int(
        state,
        "fix_gaps_timeout_seconds",
        "fixGapsTimeoutSeconds",
        "WEB_DEV_FIX_GAPS_TIMEOUT_SECONDS",
        600,
        minimum=120,
        maximum=3600,
    )
    result = runtime.run_agentic(
        task=prompt,
        system_prompt=FIX_GAPS_SYSTEM,
        cwd=_agentic_cwd(runtime, state.get("repo_path") or None),
        max_turns=fix_gaps_max_turns,
        timeout=fix_gaps_timeout,
        plugin_manager=state.get("_plugin_manager"),
        **fix_gaps_policy_kwargs,
    )
    _record_agentic_step_gate(state, step="fix_gaps", policy=fix_gaps_policy, result=result)
    changed_after = _repo_changed_file_set(state.get("repo_path", ""), state)
    partial_progress = _agentic_partial_progress_detected(
        result,
        changed_before=changed_before,
        changed_after=changed_after,
    )
    fix_gaps_summary = _agentic_repair_summary(result, "self-check-gap-fix") if partial_progress else result.summary
    agentic_success = bool(result.success or partial_progress)
    log.info("fix_gaps result", success=agentic_success, partial_progress=partial_progress, summary=fix_gaps_summary[:300])
    _record_timeline_step(
        state,
        step_key="wd.fixing_gaps",
        title="Web Dev fixing self-check gaps",
        lifecycle_state=LIFECYCLE_DONE if agentic_success else LIFECYCLE_FAILED,
        summary_template="Web Dev completed the self-check gap fix with success={success}.",
        summary_facts={"success": agentic_success},
        round=gap_round,
        conditional=True,
    )

    return {
        "fix_gaps_attempted": True,
        "fix_gaps_summary": fix_gaps_summary,
        "agentic_success": agentic_success,
        "changes_made": _workflow_changed_files(state),
        "test_cycles": 0,
        "build_cycles": 0,
    }


async def capture_screenshot(state: dict) -> dict:
    """Capture implementation screenshots from the production build.

    Screenshot strategy:
    - Server:   ``vite preview`` serving the production ``dist/`` build (primary).
                Falls back to ``vite dev`` when no ``dist/`` exists.
    - Browser:  Playwright Chromium (bundled, works in containers).
                Falls back to system Chrome/Chromium binary.
    - Last resort: HTML page snapshot.

    Using the production build ensures screenshots reflect the *final committed
    state* of the implementation — the same artefacts that will be deployed.
    External font requests (Google Fonts, etc.) are intercepted and aborted so
    that Playwright's ``load`` event fires quickly; React falls back to the
    system font stack for the screenshot while all layout / colour / spacing
    from Tailwind/CSS is fully applied.

    Screenshots are saved to the agent workspace directory only — they are NOT
    committed to the repository.  ``create_pr`` uploads them to GitHub via the
    release-assets CDN and embeds them in the PR description.
    """
    import subprocess
    import shutil
    import socket

    screenshot_required = _is_screenshot_required(state)
    log = _logger(state)

    if not screenshot_required:
        log.info("screenshot skipped", reason="not_required")
        return {"screenshot_captured": False, "screenshots": []}

    repo_path = state.get("repo_path", "")
    workspace_path = state.get("workspace_path", "")
    screenshot_dir = os.path.join(workspace_path, _AGENT_ID, "screenshots")
    screenshots = []

    if not repo_path or not os.path.isdir(repo_path):
        log.warn("capture_screenshot skipped — repo_path missing", repo_path=repo_path)
        if screenshot_required:
            raise RuntimeError("Required UI screenshot capture failed: repository path is missing")
        return {"screenshot_captured": False, "screenshots": []}

    try:
        os.makedirs(screenshot_dir, exist_ok=True)
    except OSError:
        pass

    log.step("capture_screenshot", screenshot_dir=screenshot_dir)
    print(f"[{_AGENT_ID}] capture_screenshot: repo_path={repo_path!r} screenshot_dir={screenshot_dir!r}")

    # --- Detect the correct URL path(s) to screenshot ---
    # Parse the app's router file to find implemented routes. This is needed
    # because a feature page is often served at a non-root path (e.g. /lessons),
    # and navigating to "/" would show a blank screen for SPA apps that define
    # only feature routes.
    def _detect_app_routes(repo_root: str, task_hint: str = "") -> list[tuple[str, str]]:
        """Return [(url_path, slug)] for the most relevant screenshot targets.

        Reads the app's main router file, extracts all non-root path definitions,
        and returns the best match(es) for the current task.  Falls back to
        [("/", "home")] when no routes can be detected.

        Args:
            repo_root: Absolute path to the repository root.
            task_hint: Jira summary / task description used to score routes.
        Returns:
            List of (url_path, slug) tuples, e.g. [("/lessons", "lessons")].
            slug is used as the filename prefix, e.g. "lessons-desktop.png".
        """
        import re as _re

        router_candidates = [
            "src/App.tsx", "src/App.jsx", "src/App.ts", "src/App.js",
            "src/app.tsx", "src/app.jsx",
            "src/router.tsx", "src/router.jsx", "src/Router.tsx", "src/Router.jsx",
            "src/routes.tsx", "src/routes.jsx", "src/Routes.tsx",
            "src/routing.tsx", "src/routing.jsx",
            "src/main.tsx", "src/main.jsx",
            "app/layout.tsx",       # Next.js App Router root
            "app/page.tsx",         # Next.js App Router index
            "pages/index.tsx",      # Next.js Pages Router
        ]

        found_routes: list[str] = []
        for candidate in router_candidates:
            fpath = os.path.join(repo_root, candidate)
            if not os.path.isfile(fpath):
                continue
            try:
                content = open(fpath, encoding="utf-8").read()
                # Extract path="..." / path='...' from Route JSX (<Route path="...">)
                paths_jsx = _re.findall(r'path=["\']([^"\'*?{}]+)["\']', content)
                # Extract string literals in router config objects: { path: "..." }
                paths_obj = _re.findall(r'path:\s*["\']([^"\'*?{}]+)["\']', content)
                for p in paths_jsx + paths_obj:
                    p = p.strip()
                    if p and p != "/" and not p.startswith("*") and p not in found_routes:
                        if not p.startswith("/"):
                            p = "/" + p
                        found_routes.append(p)
            except Exception:
                continue

        # Next.js App Router: infer routes from directory structure
        if not found_routes:
            app_dir = os.path.join(repo_root, "app")
            if os.path.isdir(app_dir):
                for entry in os.listdir(app_dir):
                    entry_path = os.path.join(app_dir, entry)
                    if os.path.isdir(entry_path) and not entry.startswith("(") and not entry.startswith("_"):
                        page_file = os.path.join(entry_path, "page.tsx")
                        if not os.path.isfile(page_file):
                            page_file = os.path.join(entry_path, "page.jsx")
                        if os.path.isfile(page_file):
                            found_routes.append("/" + entry)

        if not found_routes:
            # No feature routes detected — use root with generic "app" slug
            return [("/", "app")]

        # Score routes against task_hint to find best match
        task_lower = (task_hint or "").lower()
        if task_lower and len(found_routes) > 1:
            def _score(route: str) -> int:
                slug = route.strip("/").lower().replace("-", " ").replace("_", " ")
                if not slug:
                    return 0
                # Award 1 point per matching word
                return sum(1 for word in slug.split() if word and word in task_lower)

            scored = sorted(found_routes, key=_score, reverse=True)
            best_score = _score(scored[0])
            if best_score > 0:
                found_routes = [r for r in scored if _score(r) == best_score]
                if not found_routes:
                    found_routes = scored[:1]

        # Build (url_path, slug) tuples — at most 2 routes to keep PR concise
        result: list[tuple[str, str]] = []
        for route in found_routes[:2]:
            slug = route.strip("/").replace("/", "-") or "app"
            # Sanitize slug: keep alphanumeric, hyphens, underscores
            slug = _re.sub(r"[^a-z0-9\-_]", "-", slug.lower())
            result.append((route, slug))
        return result

    # Gather task context to help route scoring
    _jira_ctx = state.get("jira_context", {})
    _jira_summary = (
        _jira_ctx.get("summary")
        or (_jira_ctx.get("fields") or {}).get("summary", "")
        or state.get("stitch_screen_name", "")
        or state.get("user_request", "")
    )
    _detected_routes = _detect_app_routes(repo_path, _jira_summary)
    print(f"[{_AGENT_ID}] detected screenshot routes: {_detected_routes!r} (hint={_jira_summary[:60]!r})")

    # Use first detected route for desktop/mobile pair
    _primary_route, _feature_slug = _detected_routes[0] if _detected_routes else ("/", "app")
    _round = state.get("revision_round", state.get("revision_count", 0) + 1) or 1
    desktop_png = os.path.join(screenshot_dir, f"{_feature_slug}-desktop-{_round}.png")
    mobile_png = os.path.join(screenshot_dir, f"{_feature_slug}-mobile-{_round}.png")

    # Pick an ephemeral port to avoid conflicts with any server started by run_tests.
    def _free_port(preferred: int = 5179) -> int:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]
        except Exception:
            return preferred

    PORT = _free_port()
    dev_proc = None

    try:
        import time as _time
        import urllib.request

        # --- Step 1: Kill any leftover process on chosen port (safety net) ---
        subprocess.run(
            ["bash", "-c", f"lsof -ti:{PORT} | xargs kill -9 2>/dev/null || true"],
            timeout=5, capture_output=True,
        )

        # --- Step 2: Decide server mode: vite preview (prod build) or vite dev ---
        dist_dir = os.path.join(repo_path, "dist")
        use_preview = os.path.isdir(dist_dir)

        if not use_preview:
            # run_tests should have built dist/; if missing, try to build now.
            print(f"[{_AGENT_ID}] dist/ not found — running npm run build before screenshot")
            node_modules = os.path.join(repo_path, "node_modules")
            if not os.path.isdir(node_modules):
                subprocess.run(
                    ["npm", "install", "--prefer-offline"],
                    cwd=repo_path, timeout=120, capture_output=True,
                )
            build_result = subprocess.run(
                ["npm", "run", "build"],
                cwd=repo_path, timeout=300, capture_output=True, text=True,
            )
            use_preview = build_result.returncode == 0 and os.path.isdir(dist_dir)
            if not use_preview:
                print(f"[{_AGENT_ID}] build failed (rc={build_result.returncode}) — "
                      f"falling back to vite dev")
                # Ensure deps are present for dev server
                node_modules = os.path.join(repo_path, "node_modules")
                if not os.path.isdir(node_modules):
                    subprocess.run(
                        ["npm", "install", "--prefer-offline"],
                        cwd=repo_path, timeout=120, capture_output=True,
                    )
        else:
            print(f"[{_AGENT_ID}] dist/ found — using vite preview for production-accurate screenshot")

        # --- Step 3: Start server (preview preferred, dev fallback) ---
        if use_preview:
            # vite preview serves the production dist/ build
            server_cmd = ["npx", "vite", "preview", "--port", str(PORT), "--host", "0.0.0.0"]
        else:
            server_cmd = ["npm", "run", "dev", "--", "--port", str(PORT), "--host", "0.0.0.0"]

        dev_proc = subprocess.Popen(
            server_cmd,
            cwd=repo_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        server_type = "preview" if use_preview else "dev"
        print(f"[{_AGENT_ID}] {server_type} server started (pid={dev_proc.pid}) on port {PORT}")

        # --- Step 4: Wait for server ready (up to 60s) ---
        server_ready = False
        for _ in range(30):
            _time.sleep(2)
            try:
                conn = urllib.request.urlopen(f"http://localhost:{PORT}", timeout=3)
                if conn.status < 500:
                    server_ready = True
                    conn.close()
                    break
                conn.close()
            except Exception:
                pass
        print(f"[{_AGENT_ID}] Server ready={server_ready} (type={server_type})")

        if not server_ready:
            print(f"[{_AGENT_ID}] Server not ready in 60s — skipping screenshot")
        else:
            # --- Step 5: Take screenshots (playwright primary, Chrome fallback) ---
            playwright_ok = False
            try:
                from playwright.async_api import async_playwright
                print(f"[{_AGENT_ID}] Taking screenshots with playwright "
                      f"(wait_until=load, fonts with timeout)...")
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                    ])

                    async def _take_page_screenshot(
                        browser_inst,
                        viewport: dict,
                        out_path: str,
                        url_path: str = "/",
                        is_mobile: bool = False,
                    ) -> bool:
                        """Navigate to url_path, wait for render, screenshot; return True on success."""
                        ctx = await browser_inst.new_context(
                            viewport=viewport,
                            is_mobile=is_mobile,
                        )
                        pg = await ctx.new_page()
                        console_errors: list[str] = []
                        page_errors: list[str] = []

                        def _capture_console(msg):
                            try:
                                if msg.type in {"error", "warning"}:
                                    console_errors.append(msg.text)
                            except Exception:
                                pass

                        def _capture_page_error(exc):
                            try:
                                page_errors.append(str(exc))
                            except Exception:
                                pass

                        pg.on("console", _capture_console)
                        pg.on("pageerror", _capture_page_error)
                        try:
                            # No font blocking — allow all requests through so the browser
                            # loads Google Fonts normally (Work Sans, Newsreader, Material
                            # Symbols). Wait for the Font Loading API as well so screenshot
                            # evidence reflects the real rendered icon glyphs instead of the
                            # raw icon token text.

                            # Navigate to the detected feature URL (not just root).
                            # For React Router SPAs, vite preview serves all routes
                            # via the same index.html; the client-side router handles
                            # the path.  For vite dev server, --host already enables this.
                            target_url = f"http://localhost:{PORT}{url_path}"
                            print(f"[{_AGENT_ID}] Playwright navigating to {target_url!r}")
                            # Use 'load' (not 'networkidle') — safe with blocked CDNs.
                            await pg.goto(
                                target_url,
                                wait_until="load",
                                timeout=30000,
                            )
                            try:
                                await pg.evaluate(
                                    """async () => {
                                        if (document.fonts && document.fonts.ready) {
                                            await document.fonts.ready;
                                        }
                                    }"""
                                )
                            except Exception:
                                pass
                            # Wait for React to hydrate and CSS animations to settle.
                            # Use a longer wait for production builds via vite preview
                            # since the bundled JS needs to parse + execute + render.
                            await pg.wait_for_timeout(5000)
                            # Best-effort: verify the React root has rendered content.
                            # IMPORTANT: wait_for_selector with state="visible" returns
                            # immediately if the element exists but is empty. We must
                            # actively check for non-empty content.
                            root = pg.locator("#root")
                            try:
                                await root.wait_for(state="attached", timeout=5000)
                            except Exception:
                                pass

                            metrics = await pg.evaluate(
                                """() => {
                                    const root = document.querySelector('#root');
                                    const body = document.body;
                                    const rect = body ? body.getBoundingClientRect() : { width: 0, height: 0 };
                                    const visibleText = ((body && body.innerText) || '').replace(/\\s+/g, ' ').trim();
                                    return {
                                        rootChildren: root ? root.children.length : 0,
                                        bodyChildren: body ? body.children.length : 0,
                                        visibleTextChars: visibleText.length,
                                        bodyWidth: Math.round(rect.width || 0),
                                        bodyHeight: Math.round(rect.height || 0),
                                        title: document.title || '',
                                        readyState: document.readyState || '',
                                    };
                                }"""
                            )
                            if not _rendered_page_has_content(metrics):
                                await pg.wait_for_timeout(4000)
                                metrics = await pg.evaluate(
                                    """() => {
                                        const root = document.querySelector('#root');
                                        const body = document.body;
                                        const rect = body ? body.getBoundingClientRect() : { width: 0, height: 0 };
                                        const visibleText = ((body && body.innerText) || '').replace(/\\s+/g, ' ').trim();
                                        return {
                                            rootChildren: root ? root.children.length : 0,
                                            bodyChildren: body ? body.children.length : 0,
                                            visibleTextChars: visibleText.length,
                                            bodyWidth: Math.round(rect.width || 0),
                                            bodyHeight: Math.round(rect.height || 0),
                                            title: document.title || '',
                                            readyState: document.readyState || '',
                                        };
                                    }"""
                                )

                            if not _rendered_page_has_content(metrics):
                                print(
                                    f"[{_AGENT_ID}] Screenshot rejected due to blank/unrendered page: "
                                    f"route={url_path!r} metrics={metrics!r} console_errors={console_errors[-5:]} "
                                    f"page_errors={page_errors[-5:]}"
                                )
                                return False

                            await pg.screenshot(path=out_path, full_page=False)
                        finally:
                            await ctx.close()
                        return os.path.isfile(out_path) and os.path.getsize(out_path) > 0

                    # Desktop
                    d_ok = await _take_page_screenshot(
                        browser,
                        {"width": 1280, "height": 900},
                        desktop_png,
                        url_path=_primary_route,
                    )
                    if d_ok:
                        screenshots.append(desktop_png)
                        print(f"[{_AGENT_ID}] Playwright desktop: "
                              f"{os.path.getsize(desktop_png)} bytes")
                        playwright_ok = True

                    # Mobile
                    m_ok = await _take_page_screenshot(
                        browser,
                        {"width": 375, "height": 812},
                        mobile_png,
                        url_path=_primary_route,
                        is_mobile=True,
                    )
                    if m_ok:
                        screenshots.append(mobile_png)
                        print(f"[{_AGENT_ID}] Playwright mobile: "
                              f"{os.path.getsize(mobile_png)} bytes")

                    await browser.close()

            except ImportError:
                print(f"[{_AGENT_ID}] playwright not available — trying system Chrome")
            except Exception as pw_exc:
                print(f"[{_AGENT_ID}] playwright failed: {pw_exc} — trying system Chrome")

            # --- Chrome fallback (host machine only — not available in all containers) ---
            if not playwright_ok:
                _CHROME_CANDIDATES = [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "/Applications/Chromium.app/Contents/MacOS/Chromium",
                    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
                ]
                chrome_bin = None
                for candidate in _CHROME_CANDIDATES:
                    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                        chrome_bin = candidate
                        break
                    if "/" not in candidate and shutil.which(candidate):
                        chrome_bin = shutil.which(candidate)
                        break
                print(f"[{_AGENT_ID}] Chrome binary: {chrome_bin!r}")

                if chrome_bin:
                    # Give more time for the production build to fully render.
                    # The vite preview server is faster to render than dev, so 12s
                    # is sufficient for React hydration + Tailwind CSS application.
                    print(f"[{_AGENT_ID}] Waiting 12s for full render (Chrome fallback, "
                          f"{server_type} server)...")
                    _time.sleep(12)

                    _chrome_flags = [
                        "--headless=new", "--no-sandbox", "--disable-gpu",
                        "--disable-dev-shm-usage",
                        "--run-all-compositor-stages-before-draw",
                        # Keep Google Fonts/Material Symbols reachable so UI screenshots
                        # reflect the same icon/font rendering that the real page uses.
                        # Only block unrelated font CDNs that sometimes hang in CI.
                        "--host-rules=MAP use.typekit.net 127.0.0.1",
                    ]
                    for _out_path, _size in [(desktop_png, "1280,900"), (mobile_png, "375,812")]:
                        chrome_result = subprocess.run(
                            [chrome_bin] + _chrome_flags + [
                                f"--screenshot={_out_path}",
                                f"--window-size={_size}",
                                f"http://localhost:{PORT}{_primary_route}",
                            ],
                            capture_output=True, timeout=60,
                        )
                        if os.path.isfile(_out_path) and os.path.getsize(_out_path) > 0:
                            screenshots.append(_out_path)
                            print(f"[{_AGENT_ID}] Chrome screenshot saved ({_size}): "
                                  f"{os.path.getsize(_out_path)} bytes")
                        else:
                            print(f"[{_AGENT_ID}] Chrome screenshot failed "
                                  f"(rc={chrome_result.returncode})")
                else:
                    print(f"[{_AGENT_ID}] No Chrome/Chromium found — falling back to HTML snapshot")

        # --- HTML fallback if no screenshots and server is up ---
        if not screenshots and server_ready:
            try:
                with urllib.request.urlopen(f"http://localhost:{PORT}{_primary_route}", timeout=5) as r:
                    html_bytes = r.read()
                html_fallback = os.path.join(screenshot_dir, f"{_feature_slug}-page.html")
                with open(html_fallback, "wb") as fh:
                    fh.write(html_bytes)
                screenshots.append(html_fallback)
                print(f"[{_AGENT_ID}] HTML fallback saved: {len(html_bytes)} bytes")
            except Exception as exc:
                print(f"[{_AGENT_ID}] HTML fallback also failed: {exc}")

    except Exception as exc:
        print(f"[{_AGENT_ID}] capture_screenshot error (non-fatal): {exc}")
    finally:
        # Always stop the server
        if dev_proc and dev_proc.poll() is None:
            dev_proc.terminate()
            try:
                dev_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                dev_proc.kill()
        subprocess.run(
            ["bash", "-c", f"lsof -ti:{PORT} | xargs kill -9 2>/dev/null || true"],
            timeout=5, capture_output=True,
        )

    captured = bool(screenshots) and any(s.endswith(".png") for s in screenshots)
    log.info("screenshot result", captured=captured, count=len(screenshots),
             server_type=locals().get("server_type", "unknown"))
    if screenshot_required and not captured:
        raise RuntimeError(
            "Required UI screenshot capture failed: no PNG screenshots were produced"
        )
    return {
        "screenshot_captured": captured,
        "screenshots": screenshots,
    }


async def update_jira(state: dict) -> dict:
    """Update Jira after development is complete."""
    log = _logger(state)
    log.node("update_jira")
    jira_context = state.get("jira_context", {})
    jira_key = (
        jira_context.get("key")
        or jira_context.get("ticket_key")
        or state.get("jira_key", "")
    )

    if not jira_key:
        return {"jira_updated": False, "jira_update_skipped": "no_jira_key"}

    pr_url = state.get("pr_url", "N/A")
    branch = state.get("branch_name", "N/A")
    test_results = state.get("test_results", {})
    test_status = state.get("test_status", "unknown")
    assessment = state.get("self_assessment", {})
    changes = state.get("changes_made", [])
    _task_id = state.get("_task_id", "unknown")

    # --- Assign the ticket to the token owner ---
    task_id = state.get("_task_id", "")
    try:
        user_result = _call_boundary_tool(
            state, "jira_get_token_user", {"task_id": task_id}
        )
        user = user_result.get("user", {})
        account_id = user.get("accountId", "") or user.get("account_id", "")
        if account_id:
            log.info(
                "assigning jira ticket to token user",
                jira_key=jira_key,
                account_id_present=True,
            )
            _call_boundary_tool(
                state, "jira_update",
                {
                    "ticket_key": jira_key,
                    "fields": {"assignee": {"accountId": account_id}},
                    "task_id": task_id,
                },
            )
            log.info("jira assignee updated", jira_key=jira_key)
        else:
            log.warn("jira_get_token_user returned no accountId", user_present=bool(user))
    except Exception as exc:
        log.warn("jira assignee update skipped", error=str(exc))

    # Build test summary (accurate from actual results or test_status)
    if test_status == "skip":
        test_summary = "Skipped (max cycles reached)"
    elif test_status == "pass":
        passed = test_results.get("passed", "?")
        failed = test_results.get("failed", 0)
        test_summary = f"{passed} passed, {failed} failed"
    else:
        passed = test_results.get("passed", 0)
        failed = test_results.get("failed", "?")
        test_summary = f"{passed} passed, {failed} failed"

    score = assessment.get("score", "N/A")
    verdict = assessment.get("verdict", "N/A")
    score_str = f"{score:.2f}" if isinstance(score, float) else str(score)

    # Build the comment using inline-markdown syntax.
    # The Jira client converts this to proper ADF (bold, code, hyperlinks)
    # so it renders visually in Jira Cloud — no raw asterisks or brackets shown.
    pr_link = f"[PR: {pr_url}]({pr_url})" if pr_url and pr_url != "N/A" else "N/A"

    comment_text = (
        f"✅ Development completed by web-dev agent.\n"
        f"\n"
        f"**Task ID:** {_task_id}\n"
        f"**PR:** {pr_link}\n"
        f"**Branch:** `{branch}`\n"
        f"**Test results:** {test_summary}\n"
        f"**Self-assessment:** {score_str} ({verdict})\n"
        f"**Files changed:** {len(changes)}"
    )

    # Idempotency: check if comment with PR URL already exists
    log.debug("checking existing comments for idempotency", jira_key=jira_key)
    existing = _call_boundary_tool(
        state, "jira_list_comments", {"ticket_key": jira_key, "task_id": task_id}
    )
    already_commented = False
    for c in existing.get("comments", []):
        body = ""
        if isinstance(c, dict):
            body = c.get("body", "")
            if isinstance(body, dict):
                body = json.dumps(body)
        if pr_url and pr_url != "N/A" and pr_url in str(body):
            already_commented = True
            break

    if not already_commented:
        log.info("adding jira completion comment", jira_key=jira_key, pr_url=pr_url)
        _call_boundary_tool(
            state, "jira_comment",
            {"ticket_key": jira_key, "comment": comment_text, "task_id": task_id},
        )
        log.debug("jira comment added", jira_key=jira_key)
    else:
        log.info("jira comment already exists, skipped")

    # Transition to "In Review"
    log.debug("listing jira transitions for in-review", jira_key=jira_key)
    transitions_result = _call_boundary_tool(
        state, "jira_list_transitions", {"ticket_key": jira_key, "task_id": task_id}
    )
    transitions = transitions_result.get("transitions", [])
    _IN_REVIEW_NAMES = {
        "in review", "review", "code review", "ready for review",
        "pending review", "awaiting review",
    }
    in_review_match = next(
        (t for t in transitions
         if isinstance(t, dict) and t.get("name", "").lower() in _IN_REVIEW_NAMES),
        None,
    )
    can_review = bool(in_review_match)
    if can_review:
        _call_boundary_tool(
            state, "jira_transition",
            {"ticket_key": jira_key, "transition_name": in_review_match["name"],
             "task_id": task_id},
        )
        log.info("jira transitioned to in review", jira_key=jira_key)
    else:
        log.warn("no in-review transition available", jira_key=jira_key)

    # Write jira-update-log.json to workspace
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        import time as _time
        agent_dir = os.path.join(workspace_path, _AGENT_ID)
        os.makedirs(agent_dir, exist_ok=True)
        try:
            log_file = os.path.join(agent_dir, "jira-update-log.json")
            with open(log_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "web-dev",
                        "step": "update_jira",
                        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": {
                        "jira_key": jira_key,
                        "pr_url": pr_url,
                        "comment_added": not already_commented,
                        "transition_attempted": can_review,
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    return {"jira_updated": True, "jira_in_review": can_review}


def _load_pr_description_template() -> str:
    template_path = os.path.join(os.path.dirname(__file__), "templates", "pr_description.md")
    try:
        with open(template_path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _check_pr_status_conflict(state: dict, repo_url: str, pr_number: int) -> dict[str, Any]:
    """Check for PR status conflicts (merged, closed, or conflicting).

    Returns a dict with keys:
      - conflict: bool (True if a conflict was detected)
      - conflict_type: str (merged, closed, has_conflicts, none)
      - message: str (human-readable explanation)
    """
    if not repo_url or not pr_number:
        return {"conflict": False, "conflict_type": "none", "message": ""}

    pr_info = _call_boundary_tool(state, "scm_get_pr_info", {
        "repo_url": repo_url,
        "pr_number": int(pr_number),
        "task_id": state.get("_task_id", ""),
    })
    if pr_info.get("error"):
        return {"conflict": False, "conflict_type": "none", "message": f"Could not check PR status: {pr_info.get('error')}"}

    pr_state = pr_info.get("state", "").lower()
    if pr_state == "merged":
        return {
            "conflict": True,
            "conflict_type": "merged",
            "message": f"PR #{pr_number} has already been merged externally.",
        }
    elif pr_state == "closed":
        return {
            "conflict": True,
            "conflict_type": "closed",
            "message": f"PR #{pr_number} has been closed externally.",
        }

    # Check for merge conflicts via mergeable state if available
    mergeable = pr_info.get("mergeable")
    if mergeable is False:
        return {
            "conflict": True,
            "conflict_type": "has_conflicts",
            "message": f"PR #{pr_number} has merge conflicts with the target branch.",
        }

    return {"conflict": False, "conflict_type": "none", "message": ""}


def _attempt_rebase(repo_path: str, base_branch: str = "main") -> dict[str, Any]:
    """Attempt to rebase the current branch onto the target branch.

    Returns {"success": bool, "message": str}.
    """
    if not repo_path or not os.path.isdir(repo_path):
        return {"success": False, "message": "repo_path not available"}
    try:
        from framework.env_utils import build_isolated_git_env
        git_env = build_isolated_git_env(scope="web-dev-rebase")

        # Fetch latest
        subprocess.run(
            ["git", "fetch", "origin", base_branch],
            cwd=repo_path, env=git_env,
            capture_output=True, text=True, timeout=30, check=False,
        )

        # Try rebase
        result = subprocess.run(
            ["git", "rebase", f"origin/{base_branch}"],
            cwd=repo_path, env=git_env,
            capture_output=True, text=True, timeout=60, check=False,
        )
        if result.returncode == 0:
            return {"success": True, "message": "Rebase successful"}

        # Abort failed rebase
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=repo_path, env=git_env,
            capture_output=True, text=True, timeout=10, check=False,
        )
        return {"success": False, "message": f"Rebase failed: {result.stderr.strip()[:200]}"}
    except Exception as exc:
        return {"success": False, "message": f"Rebase error: {exc}"}


async def create_pr(state: dict) -> dict:
    """Generate a PR description and create the pull request via SCM tools."""
    runtime = state.get("_runtime")
    log = _logger(state)
    log.node("create_pr", branch=state.get("branch_name", ""))

    if not runtime:
        return {
            "pr_url": "",
            "pr_number": 0,
            "pr_title": "Implement changes",
            "commit_hash": "",
        }

    # --- Revision mode: push new commits + add PR comment, skip PR creation ---
    revision_mode = state.get("revision_mode", False)
    existing_pr_url = state.get("existing_pr_url", "")
    existing_pr_number = state.get("existing_pr_number", 0)
    if revision_mode and existing_pr_url and existing_pr_number:
        repo_path = state.get("repo_path", "")
        branch_name = state.get("branch_name", "feature/task")
        jira_ctx = state.get("jira_context", {})
        jira_key = (
            jira_ctx.get("key") or jira_ctx.get("ticket_key") or ""
            if isinstance(jira_ctx, dict) else ""
        )
        task_id = state.get("_task_id", "")

        # Check PR status conflicts before pushing
        pr_conflict = _check_pr_status_conflict(
            state, state.get("repo_url", ""), int(existing_pr_number)
        )
        if pr_conflict.get("conflict"):
            conflict_type = pr_conflict["conflict_type"]
            conflict_msg = pr_conflict["message"]
            log.warn("PR status conflict detected", type=conflict_type, conflict_message=conflict_msg)
            if conflict_type == "merged":
                # PR already merged externally — report as success
                return {
                    "pr_url": existing_pr_url,
                    "pr_number": int(existing_pr_number),
                    "pr_title": state.get("pr_title", ""),
                    "commit_hash": "",
                    "pr_conflict": conflict_type,
                    "pr_conflict_message": conflict_msg,
                }
            elif conflict_type == "closed":
                # PR closed externally — escalate to Team Lead
                raise RuntimeError(f"PR conflict: {conflict_msg}")
            elif conflict_type == "has_conflicts":
                # Attempt rebase (max 1 attempt)
                rebase_result = _attempt_rebase(repo_path, "main")
                if not rebase_result["success"]:
                    raise RuntimeError(
                        f"PR has merge conflicts and rebase failed: {rebase_result['message']}"
                    )
                log.info("rebase successful after conflict detection")

        committed_files = _git_commit_all_pending(repo_path, jira_key or "task")
        existing_changes = state.get("changes_made", [])
        branch_changes = _git_branch_changed_files(repo_path)
        all_changes = sorted(set(existing_changes) | set(committed_files) | set(branch_changes))

        # Push commits to existing branch
        log.info("revision mode: pushing to existing branch", branch=branch_name)
        push_payload = _call_boundary_tool(
            state, "scm_push", {"repo_path": repo_path, "branch": branch_name, "task_id": task_id}
        )
        if push_payload.get("error"):
            log.error("scm_push failed in revision mode", error=push_payload["error"])
            detail = str(push_payload.get("detail", "")).strip()
            raise RuntimeError(
                "Revision push failed: "
                f"{push_payload['error']}"
                f" ({detail})" if detail else f"Revision push failed: {push_payload['error']}"
            )

        # Add PR comment with revision summary
        revision_feedback = state.get("revision_feedback", "")
        test_summary = ""
        test_results = state.get("test_results") or state.get("test_output", "")
        if isinstance(test_results, dict):
            test_summary = f"\n\nTest results: {'PASS' if test_results.get('passed') else 'FAIL'}"
        elif test_results:
            test_summary = f"\n\nTest results: {str(test_results)[:500]}"
        comment_body = (
            f"**Revision update** (round {state.get('revision_round', state.get('revision_count', 1))})\n\n"
            f"Changes addressed:\n{revision_feedback[:2000]}\n\n"
            f"Modified files: {', '.join(all_changes[:15]) or 'various files'}"
            f"{test_summary}"
        )
        _call_boundary_tool(
            state, "scm_add_pr_comment",
            {
                "repo_url": state.get("repo_url", ""),
                "pr_number": int(existing_pr_number),
                "comment": comment_body,
                "task_id": task_id,
            },
        )
        log.info("revision mode: PR comment added", pr_number=existing_pr_number)

        # Upload revision screenshots as PR comment (not description)
        _screenshots = state.get("screenshots", [])
        _png_screenshots = [s for s in _screenshots if s.endswith(".png") and os.path.isfile(s)]
        _screenshot_uploaded = False
        if _png_screenshots:
            screenshot_entries = []
            for _png in _png_screenshots:
                _fname = os.path.basename(_png)
                _label = "Desktop" if "desktop" in _png.lower() else "Mobile"
                _upload_result = _call_boundary_tool(
                    state, "scm_upload_pr_image",
                    {"repo_url": state.get("repo_url", ""), "pr_number": int(existing_pr_number),
                     "image_path": _png, "task_id": task_id},
                )
                _cdn_url = _upload_result.get("image_url", "")
                if _cdn_url:
                    screenshot_entries.append(f"**{_label}**\n\n![]({_cdn_url})")
            if screenshot_entries:
                _call_boundary_tool(
                    state, "scm_add_pr_comment",
                    {"repo_url": state.get("repo_url", ""), "pr_number": int(existing_pr_number),
                     "comment": f"**Revision screenshots (round {state.get('revision_round', state.get('revision_count', 1))})**\n\n" + "\n\n".join(screenshot_entries),
                     "task_id": task_id},
                )
                _screenshot_uploaded = True

        return {
            "pr_url": existing_pr_url,
            "pr_number": int(existing_pr_number),
            "pr_title": state.get("pr_title", ""),
            "commit_hash": "",
            "screenshot_included": state.get("screenshot_captured", False),
            "screenshot_uploaded": _screenshot_uploaded,
        }

    screenshot_required = _is_screenshot_required(state)
    if screenshot_required and not state.get("screenshot_captured"):
        raise RuntimeError("Cannot create PR for a UI task without captured PNG screenshots")

    from agents.web_dev.prompts import PR_DESCRIPTION_SYSTEM, PR_DESCRIPTION_TEMPLATE

    jira_ctx = state.get("jira_context", {})
    jira_key = (
        jira_ctx.get("key") or jira_ctx.get("ticket_key") or ""
        if isinstance(jira_ctx, dict) else ""
    )
    task_id = state.get("_task_id", "")

    # Step 1: Commit any pending files and resolve the full changeset FIRST.
    repo_path = state.get("repo_path", "")
    repo_url = state.get("repo_url", "")
    branch_name = state.get("branch_name", "feature/task")

    committed_files = _git_commit_all_pending(repo_path, jira_key or "task")
    existing_changes = state.get("changes_made", [])
    branch_changes = _git_branch_changed_files(repo_path)
    all_changes = sorted(set(existing_changes) | set(committed_files) | set(branch_changes))

    if not all_changes:
        raise RuntimeError(
            f"[{_AGENT_ID}] create_pr: No file changes detected on branch {branch_name!r}. "
            "implement_changes produced 0 commits — cannot create a PR against main."
        )

    # Step 2: Generate PR description (single-shot LLM)
    _assessment = state.get("self_assessment", {})
    _test_results = state.get("test_results", {})
    _screenshots = state.get("screenshots", [])
    _jira_url = ""
    if jira_key:
        _jira_ctx = state.get("jira_context", {})
        if isinstance(_jira_ctx, dict):
            jira_base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
            _jira_url = _jira_ctx.get("url", "") or (f"{jira_base_url}/browse/{jira_key}" if jira_base_url else "")
    pr_template = _load_pr_description_template()
    desc_prompt = PR_DESCRIPTION_TEMPLATE.format(
        user_request=state.get("user_request", ""),
        branch_name=branch_name,
        jira_key=jira_key or "N/A",
        jira_url=_jira_url or "N/A",
        implementation_summary=state.get("implementation_summary", ""),
        changed_files=", ".join(all_changes[:20]) or "various files",
        test_status=state.get("test_status", "unknown"),
        test_results=json.dumps(_test_results),
        assessment_score=_assessment.get("score", "N/A"),
        assessment_verdict=_assessment.get("verdict", "N/A"),
        assessment_gaps=", ".join(_assessment.get("gaps", [])) or "none",
        screenshot_paths=", ".join(_screenshots) or "none captured",
        pr_description_template=pr_template or "Use the standard Constellation PR sections.",
    )
    desc_result = runtime.run(desc_prompt, system_prompt=PR_DESCRIPTION_SYSTEM,
                              plugin_manager=state.get("_plugin_manager"))
    pr_meta = _safe_json(desc_result.get("raw_response", ""), fallback={})
    pr_title = pr_meta.get("title", "Implement task changes")
    pr_description = pr_meta.get("description", state.get("implementation_summary", ""))

    # Step 2.5: Prepare screenshot artifacts. Screenshots must be hosted outside
    # the PR branch and injected into the PR description only after PR creation.
    _screenshots = state.get("screenshots", [])
    _png_screenshots = [s for s in _screenshots if s.endswith(".png") and os.path.isfile(s)]
    _screenshot_uploaded = False
    _screenshot_section = ""
    if screenshot_required and not _png_screenshots:
        raise RuntimeError("Cannot create PR for a UI task because PNG screenshots are missing")

    pr_description = pr_description.rstrip()

    # Step 3: Push branch then create PR via SCM boundary tools (not open agentic).
    task_id = state.get("_task_id", "")

    log.info("pushing branch to remote", branch=branch_name)
    push_payload = _call_boundary_tool(
        state, "scm_push", {"repo_path": repo_path, "branch": branch_name, "task_id": task_id}
    )
    if push_payload.get("error"):
        log.error("scm_push failed", error=push_payload["error"])
        print(f"[{_AGENT_ID}] scm_push failed: {push_payload['error']}")
        detail = str(push_payload.get("detail", "")).strip()
        message = f"scm_push failed: {push_payload['error']}"
        if detail:
            message += f" ({detail})"
        raise RuntimeError(message)
    else:
        log.debug("scm_push ok", branch=branch_name)
        print(f"[{_AGENT_ID}] scm_push OK: branch={branch_name!r}")

    log.info("creating PR", source_branch=branch_name, target="main", title=pr_title[:80])
    pr_payload = _call_boundary_tool(
        state, "scm_create_pr",
        {
            "repo_url": repo_url,
            "source_branch": branch_name,
            "target_branch": "main",
            "title": pr_title,
            "description": pr_description,
            "task_id": task_id,
        },
    )
    pr_url = pr_payload.get("prUrl") or pr_payload.get("pr_url", "")
    pr_number = pr_payload.get("prNumber") or pr_payload.get("pr_number", 0)
    if not pr_number and isinstance(pr_payload.get("pr"), dict):
        pr_number = pr_payload["pr"].get("number") or pr_payload["pr"].get("id") or 0
    if not pr_number and pr_url and "/pull/" in pr_url:
        try:
            pr_number = int(pr_url.rstrip("/").rsplit("/pull/", 1)[1])
        except (TypeError, ValueError):
            pr_number = 0
    commit_hash = pr_payload.get("commitHash") or pr_payload.get("commit_hash", "")
    pr_status = pr_payload.get("status", "")
    pr_error = pr_payload.get("error", "")
    if not pr_url and (pr_error or (pr_status and pr_status != "ok")):
        log.error("PR creation failed", status=pr_status, error=pr_error)
        print(f"[{_AGENT_ID}] create_pr FAILED: status={pr_status!r} error={pr_error!r} payload={pr_payload}")
    else:
        log.info("PR created", pr_url=pr_url, branch=branch_name)
        print(f"[{_AGENT_ID}] create_pr done: prUrl={pr_url!r} prNumber={pr_number} status={pr_status!r}")

    from framework.validation_gates import validate_pr_created, validate_screenshot_upload
    pr_gate = validate_pr_created(pr_url, int(pr_number or 0) if pr_number else None)
    if not pr_gate.passed:
        raise RuntimeError(f"PR creation gate failed: {pr_gate.feedback}")

    # Step 4: Upload screenshots to GitHub CDN and PATCH the PR description.
    _first_screenshot_url = ""
    if _png_screenshots:
        log.info("uploading screenshots to CDN", screenshots=len(_png_screenshots), pr_number=pr_number)
        _screenshot_entries: list[tuple[str, str]] = []
        for _png in _png_screenshots:
            _fname = os.path.basename(_png)
            _label = "Desktop (1280×900)" if "desktop" in _png.lower() else "Mobile (375×812)"
            _upload_result = _call_boundary_tool(
                state,
                "scm_upload_pr_image",
                {
                    "repo_url": repo_url,
                    "pr_number": int(pr_number or 0),
                    "image_path": _png,
                    "task_id": task_id,
                },
            )
            _cdn_url = _upload_result.get("image_url", "")
            if _cdn_url:
                _screenshot_entries.append((_label, _cdn_url))
                _first_screenshot_url = _first_screenshot_url or _cdn_url
                continue

            log.error("screenshot upload failed", screenshot=_fname, error=_upload_result.get("error", ""))
            print(f"[{_AGENT_ID}] CDN upload failed for {_fname}: "
                  f"{_upload_result.get('error', '(no error detail)')}")

        if _screenshot_entries:
            _section_parts = [
                f"**{_lbl}**\n\n![]({_url})" for _lbl, _url in _screenshot_entries
            ]
            _screenshot_section = "\n\n## Screenshots\n\n" + "\n\n".join(_section_parts)
            updated_description = pr_description + _screenshot_section
            update_payload = _call_boundary_tool(
                state,
                "scm_update_pr",
                {
                    "repo_url": repo_url,
                    "pr_number": int(pr_number or 0),
                    "description": updated_description,
                    "title": pr_title,
                    "task_id": task_id,
                },
            )
            if update_payload.get("error") or update_payload.get("status") not in ("ok", "no_changes", ""):
                log.error("scm_update_pr failed", error=update_payload.get("error", ""), status=update_payload.get("status", ""))
                raise RuntimeError("Cannot finalize PR for a UI task because screenshot URLs could not be added to the PR description")

            pr_description = updated_description
            _screenshot_uploaded = True
            log.info("screenshot PR description updated", screenshots=len(_screenshot_entries), pr_number=pr_number)
            print(f"[{_AGENT_ID}] Screenshots uploaded to GitHub CDN — "
                  f"{len(_screenshot_entries)} image(s) embedded in PR description")
        elif screenshot_required:
            raise RuntimeError("Cannot finalize PR for a UI task because screenshot upload did not return CDN URLs")

        print(f"[{_AGENT_ID}] {len(_png_screenshots)} screenshot(s) processed for PR description")

    screenshot_gate = validate_screenshot_upload(
        screenshot_required=screenshot_required,
        screenshot_uploaded=_screenshot_uploaded,
        screenshot_url=_first_screenshot_url,
    )
    if not screenshot_gate.passed:
        raise RuntimeError(f"Screenshot upload gate failed: {screenshot_gate.feedback}")

    # Write pr-evidence.json to workspace
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        import time as _time
        agent_dir = os.path.join(workspace_path, _AGENT_ID)
        os.makedirs(agent_dir, exist_ok=True)
        try:
            evidence_file = os.path.join(agent_dir, "pr-evidence.json")
            with open(evidence_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "web-dev",
                        "step": "create_pr",
                        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": {
                        "pr_url": pr_url,
                        "pr_number": pr_number,
                        "branch": branch_name,
                        "title": pr_title,
                        "commit_hash": commit_hash,
                        "files_changed": len(all_changes),
                        "changed_files": all_changes[:30],
                        "test_status": state.get("test_status", "unknown"),
                        "self_assessment_score": state.get("self_assessment", {}).get("score", "N/A"),
                        "screenshot_included": state.get("screenshot_captured", False),
                        "screenshot_uploaded": _screenshot_uploaded,
                        "screenshots": _screenshots,
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    return {
        "pr_url": pr_url,
        "pr_number": pr_number,
        "pr_title": pr_title,
        "pr_description": pr_description,
        "commit_hash": commit_hash,
        "changes_made": all_changes,
        "screenshot_uploaded": _screenshot_uploaded,
    }


async def report_result(state: dict) -> dict:
    """Return final result summary."""
    log = _logger(state)
    log.node("report_result")
    revision_mode = bool(state.get("revision_mode") or state.get("revision_feedback"))
    step_key = "wd.handover_retry" if revision_mode else "wd.handover"
    title = "Web Dev handing over revised result" if revision_mode else "Web Dev handing over to Team Lead"
    revision_round = int(state.get("revision_round", 0) or 0)
    handover_round = max(revision_round - 1, 0) if revision_mode else 0
    _record_timeline_step(
        state,
        step_key=step_key,
        title=title,
        summary_template="Web Dev is preparing the delivery package for Team Lead.",
        round=handover_round,
        conditional=revision_mode,
    )
    pr_url = state.get("pr_url", "N/A")
    branch_name = state.get("branch_name", "N/A")
    changes = state.get("changes_made", [])
    pr_title = state.get("pr_title", "")
    test_status = state.get("test_status", "unknown")
    log.info("report_result", pr_url=pr_url, branch=branch_name,
             test_status=test_status, files_changed=len(changes))
    print(f"[{_AGENT_ID}] report_result: prUrl={pr_url!r} branch={branch_name!r} test_status={test_status!r} changes={len(changes)}")

    summary_parts = [
        f"Implementation complete.",
        f"{len(changes)} file(s) changed.",
        f"Test status: {test_status}.",
    ]
    if pr_title:
        summary_parts.append(f"PR: {pr_title}.")
    if pr_url and pr_url != "N/A":
        summary_parts.append(f"URL: {pr_url}")

    _record_timeline_step(
        state,
        step_key=step_key,
        title=title,
        lifecycle_state=LIFECYCLE_DONE,
        summary_template="Web Dev handed over the implementation with test status {test_status}.",
        summary_facts={"test_status": test_status},
        round=handover_round,
        conditional=revision_mode,
    )

    return {
        "success": True,
        "state": "TASK_STATE_COMPLETED",
        "summary": " ".join(summary_parts),
        "implementation_summary": " ".join(summary_parts),
        "pr_url": pr_url,
        "pr_number": state.get("pr_number", 0),
        "repo_url": state.get("repo_url", ""),
        "branch_name": branch_name,
        "pr_title": pr_title,
        "pr_description": state.get("pr_description", ""),
        "changes_made": changes,
        "changed_files": changes,
    }


async def pause_for_user_input(state: dict) -> dict:
    """Pause the workflow and ask the orchestrator for guidance.

    Raised after self-assessment exhausts retries with unresolved gaps.
    On resume (``_resume_value`` set by WorkflowRunner.resume()), the node
    consumes the user guidance and sets ``route = "user_responded"`` so the
    workflow loops back through implement_changes.
    """
    resume_value = state.get("_resume_value")
    if resume_value is not None:
        _record_timeline_step(
            state,
            step_key="wd.requesting_user_input",
            title="Web Dev requesting user input",
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Web Dev received user guidance and resumed the workflow.",
            conditional=True,
        )
        return {
            "revision_feedback": f"User guidance after self-assessment escalation: {resume_value}",
            "assess_cycles": 0,  # reset so the loop can run again
            "route": "user_responded",
        }

    from framework.workflow import interrupt

    assessment = state.get("self_assessment", {})
    gaps = assessment.get("gaps", [])
    gap_text = "\n".join(f"- {g}" for g in gaps[:10]) if gaps else "No specific gaps."
    if assessment.get("failure_type") == "schema":
        prompt_text = (
            "Self-assessment could not obtain valid structured output from the configured runtime.\n"
            f"Runtime/schema feedback:\n{gap_text}\n"
            "Please retry after checking the agentic backend, or provide guidance on how to proceed."
        )
    else:
        prompt_text = (
            "Self-assessment could not resolve all gaps after maximum retries.\n"
            f"Remaining gaps:\n{gap_text}\n"
            "Please review and provide guidance on how to proceed."
        )
    _record_timeline_step(
        state,
        step_key="wd.requesting_user_input",
        title="Web Dev requesting user input",
        lifecycle_state=LIFECYCLE_WAITING_FOR_USER,
        summary_template="Web Dev requested user input after unresolved self-check gaps.",
        conditional=True,
    )

    interrupt(
        prompt_text,
        assessment_score=assessment.get("score"),
        gaps=gaps,
    )

    # unreachable — interrupt() raises InterruptSignal
    return {}
