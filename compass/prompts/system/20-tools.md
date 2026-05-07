# Compass Agent — Available Tools

## Orchestration Tools (Primary)

- `dispatch_agent_task` — Dispatch a task to a downstream agent (Team Lead, Office).
- `wait_for_agent_task` — Wait for a downstream task to complete, polling for callbacks.
- `ack_agent_task` — Send ACK to a per-task agent after reviewing its output.
- `launch_per_task_agent` — Launch a per-task agent container when no idle instance is available.

## Control Tools

- `check_agent_status` — Check health status of a downstream agent before dispatching.
- `list_available_agents` — List all registered agents and their capabilities.
- `registry_query` — Query the Capability Registry for a specific capability.
- `report_progress` — Report progress milestones back to the user interface.
- `request_user_input` — Pause and request clarification from the user (INPUT_REQUIRED).
- `complete_current_task` — Mark the current task as successfully completed.
- `fail_current_task` — Mark the current task as failed with a structured error.

## Skill Tools

- `load_skill` — Load a skill playbook for guidance.
- `list_skills` — List available skills in the catalog.

## Local Workspace Evidence Tools

- `read_local_file` — Read shared-workspace evidence, logs, and summaries.
- `list_local_dir` — Inspect workspace folders before deciding what evidence is available.
- `search_local_files` — Search the shared workspace for PR URLs, branch names, or failure context.

## Compatibility Notes

- Legacy aliases `read_file`, `glob`, and `grep` still exist, but prefer the `*_local_*` names when reasoning about shared-workspace evidence.
