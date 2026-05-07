# Office Agent — Available Tools

## Local File Tools

- `read_local_file` — Read a local file from the authorized document path.
- `write_local_file` — Write a file to an authorized output location.
- `edit_local_file` — Apply a targeted edit to an existing file.
- `list_local_dir` — List directory contents within the authorized path.
- `search_local_files` — Find files matching a glob pattern in the authorized path.
- `run_local_command` — Run a shell command (e.g., `wc -l`, `head`, `ls -la`, `file`, `mv`, `mkdir -p`).

## Lifecycle / Control Tools

- `complete_current_task` — Mark the current task complete with output artifact text and file paths.
- `fail_current_task` — Mark the current task as failed with an error message.
- `report_progress` — Report a progress milestone (displayed in the UI timeline).

## Planning Tools

- `create_plan` — Create a step-by-step plan for complex tasks.
- `update_plan_step` — Mark a plan step as in-progress or done.

## Skill Tools

- `load_skill` — Load domain-specific guidance (e.g., `office-agent-workflow`).
