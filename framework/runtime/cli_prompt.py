"""Helpers for passing large prompts to subprocess-backed CLI runtimes."""
from __future__ import annotations

from contextlib import contextmanager
import os
import tempfile
from collections.abc import Iterator


_DEFAULT_ARG_PROMPT_LIMIT = 24000


def _prompt_arg_limit() -> int:
    raw = os.environ.get("CONSTELLATION_CLI_ARG_PROMPT_LIMIT", "").strip()
    if not raw:
        return _DEFAULT_ARG_PROMPT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_ARG_PROMPT_LIMIT
    return max(1024, value)


def _prompt_needs_spooling(prompt: str) -> bool:
    return len(prompt.encode("utf-8")) > _prompt_arg_limit()


@contextmanager
def cli_prompt_argument(full_prompt: str, *, backend: str) -> Iterator[str]:
    """Yield a safe prompt argument for CLI backends.

    Some agentic CLIs only expose a prompt string flag. Passing a large task
    prompt through argv can fail before the CLI starts with ``E2BIG``. For large
    prompts, write the full prompt to a temporary file and pass a short bootstrap
    instruction telling the agent to read that file.
    """
    if not _prompt_needs_spooling(full_prompt):
        yield full_prompt
        return

    with tempfile.TemporaryDirectory(prefix=f"constellation-{backend}-prompt-") as tmp_dir:
        prompt_path = os.path.join(tmp_dir, "task-prompt.md")
        with open(prompt_path, "w", encoding="utf-8") as fh:
            fh.write(full_prompt)
        yield (
            "The full task prompt is too large to pass safely as a CLI argument.\n"
            f"Full task prompt file: {prompt_path}\n"
            "Read that file completely before taking action. Treat its contents "
            "as the authoritative system and task instructions. Do not modify, "
            "delete, or commit the prompt file."
        )
