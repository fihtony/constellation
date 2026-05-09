---
name: constellation-generic-agent-workflow
description: >
  Runtime-first workflow guidance for Constellation agents. Use when an agent
  should let the selected runtime backend decide the next action through tools
  instead of following a Python-authored business workflow.
user-invocable: false
---

# Runtime-First Agent Workflow

## Core Rule

- The runtime is the decision-maker. Python host code should provide boundaries, task context, and tools, then stay out of the business workflow.
- Use tool results to determine the next step. Do not assume a fixed sequence when the task context can be inspected directly.

## Tool-First Execution Order

1. Use `todo_write` to maintain a short, current plan.
2. Use discovery tools before guessing: `registry_query`, `list_available_agents`, `check_agent_status`, `load_skill`.
3. Use local workspace tools before asking for input:
   - `read_local_file`
   - `write_local_file`
   - `edit_local_file`
   - `list_local_dir`
   - `search_local_files`
   - `run_local_command`
4. Use orchestration tools only after you know which downstream capability you need:
   - `dispatch_agent_task`
   - `wait_for_agent_task`
   - `ack_agent_task`
5. Use `request_user_input` only after boundary agents, registry discovery, and shared-workspace evidence are exhausted.

## Shared Workspace Guidance

- Treat the shared workspace as the source of truth for plans, logs, artifacts, and evidence.
- Prefer `*_local_*` tools over legacy aliases when reasoning about workspace contents.
- Keep writes narrow and auditable. Plans, summaries, and evidence metadata belong in the workspace; product-code edits belong to execution agents.

## Completion Rules

- Call `complete_current_task` only after checking the required evidence.
- Call `fail_current_task` with a clear blocker when the missing prerequisite is real and cannot be recovered through available tools.
- Always pair `dispatch_agent_task` with `wait_for_agent_task`, and send `ack_agent_task` after review or aggregation is finished.