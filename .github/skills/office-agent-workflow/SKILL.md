# Skill: Office Agent Workflow

## Purpose

This skill guides the **Office Agent** through document-processing tasks using
the agentic runtime backend. The LLM uses this guidance — combined with the
available tools — to decide every next action.

Capabilities covered:
- `office.document.summarize` / `office.folder.summarize` — read-only summarization
- `office.data.analyze` — spreadsheet and CSV data analysis
- `office.folder.organize` — folder restructuring

---

## Compass Routing Overview (context only — not for Office Agent execution)

1. Compass uses the shared agentic runtime to classify every incoming request.
2. Development work routes to `team-lead.task.analyze`.
3. Local document work routes to one of:
   - `office.document.summarize`
   - `office.folder.summarize`
   - `office.data.analyze`
   - `office.folder.organize`
4. If an office task does not include an absolute path, Compass enters `TASK_STATE_INPUT_REQUIRED`.
5. Once the path is known, Compass selects output mode:
   - `workspace` → source bind is read-only
   - `inplace` → Compass asks for write confirmation before dispatch
6. Compass launches Office Agent with metadata: `officeTargetPaths`, `officeOutputMode`, `officeInputRoot`.

---

## Office Agent Execution Workflow

The Office Agent runtime backend drives all decisions below. Python code only
handles protocol, permissions, and tool wiring.

### Step 1: Orientation

1. Call `report_progress` with `"Office Agent starting: <capability>"`
2. Use `todo_write` to record a high-level execution plan.
3. Use `list_local_dir` to understand the target structure.
4. If no target paths are available, call `fail_current_task` with a clear message.

### Step 2: File Discovery

1. Walk target paths with `list_local_dir` and `search_local_files`.
2. Collect readable files by extension: `.txt`, `.md`, `.csv`, `.json`, `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.xls`
3. Skip files > 50 MB — note them as warnings.
4. Skip macro-enabled formats (`.xlsm`, `.docm`) — note as unsupported.
5. Call `report_progress` with `"Discovered N files"`

### Extracting Text from Binary Formats

For **PDF** files (`.pdf`), use the dedicated `read_pdf` tool:
```
read_pdf(path="/path/to/file.pdf")
```

For **Word** files (`.docx`), use the dedicated `read_docx` tool:
```
read_docx(path="/path/to/file.docx")
```

For **PowerPoint** files (`.pptx`), use `read_pptx`:
```
read_pptx(path="/path/to/file.pptx")
```

For **Excel** files (`.xlsx`), use `read_xlsx`:
```
read_xlsx(path="/path/to/file.xlsx")
```

These tools are loaded by the Office Agent at startup from `office/tools/document_tools.py`.
They enforce the sandbox path jail and return extracted plain text directly.
Do NOT use `run_local_command` with inline Python to read binary documents — always prefer
the dedicated document reader tools.

### Step 3a: Summarization (office.document.summarize / office.folder.summarize)

1. Use `read_local_file` to read each file in turn.
2. For binary or large files, use `run_local_command` with `head -n 200`.
3. For each document identify: title, key topics, main findings, page count.
4. Compose a Markdown summary. For multiple docs, add a cross-document synthesis.
5. Save to `<workspace>/office-agent/summary.md` using `write_local_file`.
6. Call `report_progress` with `"Summary written"`

### Step 3b: Data Analysis (office.data.analyze)

1. Use `read_local_file` or `run_local_command` with `head -n 50` for CSV preview.
2. Identify columns, data types, value ranges, missing values.
3. Compute statistics: row count, min/max/avg for numeric columns, top categories.
4. Identify patterns, trends, anomalies relevant to the user request.
5. Write a Markdown analysis report with an overview table and key findings.
6. Save to `<workspace>/office-agent/analysis.md` using `write_local_file`.
7. Call `report_progress` with `"Analysis report written"`

### Step 3c: Folder Organization (office.folder.organize)

1. Use `list_local_dir` to enumerate all files.
2. Group files by the user's specified criterion (student name, date, file type, topic, etc.).
   Always follow the user's explicit grouping intent from the task text.
3. Write the plan to `<workspace>/office-agent/organization-plan.json` using `write_local_file`.
4. Create the organized output:
   - **WORKSPACE mode** (source bind is read-only):
     - Create `<workspace>/office-agent/organized/<group-name>/` directories.
     - For each file in a group: read its content (`read_local_file` for text, `run_local_command` for PDF/DOCX)
       and write it to `<workspace>/office-agent/organized/<group-name>/<filename>` using `write_local_file`.
     - Write `<workspace>/office-agent/organization-report.md` summarizing the groupings.
   - **IN-PLACE mode** (source bind is read-write):
     - Create subdirectories inside the source root with `run_local_command` (`mkdir -p`).
     - Move or copy files with `run_local_command` (`mv`, `cp`).
     - Never delete original files — copy first, verify, then optionally remove original.
     - Write `organization-report.md` to `<workspace>/office-agent/` for audit purposes.
5. Call `report_progress` with `"Organization complete"` or `"Plan written"`

### Step 4: Completion

1. Write `<workspace>/office-agent/warnings.md` if any files were skipped.
2. Call `collect_task_evidence` to capture output paths.
3. Call `check_definition_of_done` with an appropriate checklist.
4. Call `complete_current_task` with a concise summary and artifact paths.

---

## Error Handling

| Situation | Action |
|---|---|
| No readable files found | `fail_current_task` with explanation |
| All files too large or unsupported | `fail_current_task` with file list |
| Write permission denied (IN-PLACE) | `fail_current_task` with path and error |
| Partial success (some files failed) | Continue; note failures in warnings.md; `complete_current_task` |
| Unexpected exception | `fail_current_task` with error message |

---

## Authorization Constraints

- Only operate on the paths listed in the task's **Target Files / Directories**.
- Do not access, read, or write any files outside the authorized target paths.
- For summarize/analyze modes: never modify source files.
- Preserve originals: never delete user files.
- No external calls: do not access the internet, Jira, SCM, or any external system.
- No macro execution: never open or execute macros from `.xlsm` or `.docm` files.

---

## Supported File Formats

| Extension | Support level |
|---|---|
| `.txt`, `.md`, `.csv`, `.json` | Full — use `read_local_file` |
| `.pdf` | Full — use `read_pdf` tool |
| `.docx` | Full — use `read_docx` tool |
| `.pptx` | Full — use `read_pptx` tool |
| `.xlsx` | Full — use `read_xlsx` tool |
| `.xls` | Best-effort — use `read_xlsx` tool |
| `.doc`, `.ppt` | Unsupported — report with guidance |
| Scanned/OCR-only PDFs | Unsupported — report clearly |
| `.xlsm`, `.docm` | Rejected — macro safety boundary |

---

## Definition of Done

An Office Agent task is **complete** when ALL of the following are true:

1. At least one target file was successfully read and processed.
2. The requested output (summary/analysis/plan) was written to workspace or source directory.
3. All accessed paths were within the authorized target paths.
4. A result artifact was returned with the output file path and task summary.
5. Source files were NOT modified (for summarize/analyze modes).

---

## Key Files

| File | Purpose |
|---|---|
| `office/app.py` | A2A protocol, permission enforcement, task lifecycle |
| `office/agentic_workflow.py` | Tool names, task prompt builder, control tool wiring |
| `office/tools/document_tools.py` | Office-specific document readers (read_pdf, read_docx, etc.) |
| `office/prompts/tasks/process.md` | Task prompt template injected into run_agentic() |
| `office/prompts/system/` | Modular system prompt (role, boundaries, tools, DoD) |
| `compass/office_routing.py` | Compass-side path validation and Docker bind helpers |

---

## Validation Commands

```bash
# Unit tests for office agent agentic workflow
python -m unittest tests.test_agent_runtime_adoption -v
python -m unittest tests.test_migration_phases -v

# End-to-end through Compass
python tests/test_office_agent_e2e.py -v

# Build office agent image
./build-agents.sh office
```
