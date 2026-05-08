# Office Agent — Available Tools

## Document Reader Tools

- `read_pdf` — Extract plain text from a PDF file. Prefer this over `run_local_command` with pdfplumber.
- `read_docx` — Extract plain text from a Word document (.docx). Prefer this over `run_local_command` with python-docx.
- `read_pptx` — Extract plain text from a PowerPoint presentation (.pptx).
- `read_xlsx` — Extract data from an Excel spreadsheet (.xlsx) as CSV-formatted text.

## Local File Tools

- `read_local_file` — Read a local text file from the authorized document path.
- `write_local_file` — Write a file to an authorized output location.
- `edit_local_file` — Apply a targeted edit to an existing file.
- `list_local_dir` — List directory contents within the authorized path.
- `search_local_files` — Find files matching a glob pattern in the authorized path.
- `run_local_command` — Run a shell command (e.g., `wc -l`, `head`, `ls -la`, `file`, `mv`, `mkdir -p`).

## Lifecycle / Control Tools

- `complete_current_task` — Mark the current task complete with output artifact text and file paths.
- `fail_current_task` — Mark the current task as failed with an error message.
- `report_progress` — Report a progress milestone (displayed in the UI timeline).
- `get_task_context` — Inspect current task metadata, authorized paths, and workspace info.
- `get_agent_runtime_status` — Inspect the current runtime backend and readiness.

## Planning Tools

- `todo_write` — Record and update a concise execution plan for the current office task.

## Skill Tools

- `load_skill` — Load domain-specific guidance (e.g., `office-agent-workflow`).
- `list_skills` — List available skills before loading one dynamically.

## Validation / Evidence Tools

- `collect_task_evidence` — Capture output paths and evidence files for the result.
- `check_definition_of_done` — Verify the office task meets its completion checklist.
- `summarize_failure_context` — Produce a structured failure report when the task cannot complete.
