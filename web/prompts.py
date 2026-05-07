"""LLM prompt templates for the Web Agent.

All system prompts are defined in web/prompts/system/*.md (loaded via manifest.yaml
through build_system_prompt_from_manifest()).

All task prompts are defined in web/prompts/tasks/*.md and rendered by
web.agentic_workflow.build_web_task_prompt().

This module is intentionally minimal.  All workflow-driving prompt strings
live in the files above to enable independent versioning, review, and A/B testing.
"""
