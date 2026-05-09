"""Prompt strings for the SCM agent.

All system prompt content lives in prompts/system/*.md (loaded via build_system_prompt_from_manifest).
All task prompt content lives in prompts/tasks/*.md (loaded via build_task_prompt).
This file is intentionally minimal — add any ad-hoc string constants here only if they
cannot be expressed as a structured prompt file.
"""

# No inline LLM prompt strings. See prompts/system/ and prompts/tasks/ instead.