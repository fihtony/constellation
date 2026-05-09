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

### Progress Message Quality Rule

**MANDATORY**: Every `report_progress` call MUST include real, specific detail.
Progress messages are shown directly in the user's UI timeline — they must be
meaningful and informative, not placeholder labels.

**FORBIDDEN short/vague messages** (never use these):
- `"start"`, `"starting"`, `"begin"`, `"init"`, `"initialize"`
- `"discovery"`, `"discover"`, `"scan"`, `"scanning"`
- `"extract"`, `"extraction"`, `"read"`, `"reading"`
- `"process"`, `"processing"`, `"work"`, `"working"`
- `"done"`, `"complete"`, `"finish"`, `"finished"`
- Any single word or vague two-word phrase

**REQUIRED format**: Every progress message must answer one or more of:
- *What* is being done (specific action)
- *What files/data* are involved (names, counts, types)
- *What was found or produced* (concrete results)

Good examples:
- `"Scanning /app/userdata/stlouis: found 4 files (2 PDF, 1 DOCX, 1 TXT). Preparing to extract text."`
- `"Reading decembre-2025-bulletin.pdf (PDF, 8 pages) — extracting content with read_pdf tool"`
- `"Analysis complete: 1,200 rows, top sales rep is Alice Thompson ($148,500 total). Writing analysis.md."`
- `"Organizing 12 essays by student name: identified 4 students (Ethan, Yan, Alice, Charlie)."`

---

### Step 1: Orientation

1. Call `report_progress` with a DESCRIPTIVE message that names the capability and target, e.g.:
   `"Office Agent starting: capability=<capability>, target=<path>, output_mode=<mode>. Preparing execution plan."`
2. Use `todo_write` to record a high-level execution plan with numbered steps.
3. Use `list_local_dir` to understand the target structure.
4. If no target paths are available, call `fail_current_task` with a clear message.

### Step 2: File Discovery

1. Walk target paths with `list_local_dir` and `search_local_files`.
2. Collect readable files by extension: `.txt`, `.md`, `.csv`, `.json`, `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.xls`
3. Skip files > 50 MB — note them as warnings.
4. Skip macro-enabled formats (`.xlsm`, `.docm`) — note as unsupported.
5. Call `report_progress` with a DESCRIPTIVE message that names the files found, e.g.:
   `"Discovered 4 files in /app/userdata/stlouis: decembre-2025-bulletin.pdf, fevrier-2026-news.pdf, janvier-2026.docx, README.txt. Beginning content extraction."`

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

**Goal**: Produce a comprehensive, well-structured summary report that a reader can act on
without reading the source documents. Quality matters — do not write a shallow one-liner per file.

1. Call `report_progress` with:
   `"Extracting content from <N> documents: <list of filenames>"`
2. Read each file in turn using the appropriate tool (`read_local_file`, `read_pdf`, `read_docx`, etc.).
3. For each document produce a **detailed per-document section** containing:
   - **Title / file name**
   - **Document type and audience** (e.g. school bulletin for parents, meeting minutes, course guide)
   - **Key topics and themes** — substantive bullet points, not vague labels
   - **Important dates, deadlines, or events** — list them explicitly
   - **Action items or decisions** (if any)
   - **Notable quotes or key numbers** (if meaningful)
   - **Word/page count or row count** (for data context)
4. After all per-document sections, write a **Cross-Document Synthesis** section:
   - Common themes across documents
   - Timeline of key dates/events in chronological order
   - Overall audience and purpose of the collection
   - Any contradictions, gaps, or follow-up questions
5. Write the full report to the output destination determined by Output Mode.
   - **WORKSPACE mode**: `<workspace>/office-agent/summary.md`
   - **IN-PLACE mode**: `summary.md` in the target directory
6. Call `report_progress` with:
   `"Summary report written: <N> documents processed, <M> key dates identified, output at <path>"`

**Quality bar**: The report must be substantive enough that a reader could:
- Know the topic and purpose of each document
- Extract all key dates and events without opening the originals
- Understand follow-up actions or open questions

### Step 3b: Data Analysis (office.data.analyze)

**Goal**: Produce a polished Markdown analysis report — NOT just a JSON file.
The final deliverable MUST be a `.md` file. JSON or CSV intermediary files are
acceptable as scratch work in the workspace, but the user-facing output is always
a human-readable Markdown report.

1. Call `report_progress` with:
   `"Reading and profiling <filename>: detecting columns, data types, and row count"`
2. Use `read_local_file` or `run_local_command` with `head -n 50` for CSV preview.
3. Identify columns, data types, value ranges, missing values.
4. Compute statistics: row count, min/max/avg for numeric columns, top categories.
5. Identify patterns, trends, anomalies relevant to the user request.
6. Call `report_progress` with:
   `"Analysis complete: <N> rows, <M> columns. Identified top <entity> and <K> key insights. Writing report."`
7. Write a **Markdown analysis report** (`analysis.md`) with the following structure:
   ```markdown
   # Analysis Report: <filename or dataset name>

   ## Overview
   - **Source file**: `<filename>`
   - **Total rows**: N
   - **Columns**: list of column names

   ## Key Findings
   - Finding 1 (direct answer to the user question)
   - Finding 2
   - ...

   ## Statistical Summary
   | Column | Min | Max | Mean | Median |
   |--------|-----|-----|------|--------|
   ...

   ## Top Categories / Rankings
   (tables or bullet lists of top values per dimension)

   ## Patterns and Trends
   (narrative description of notable patterns)

   ## Methodology Notes
   (brief note on how statistics were computed)
   ```
8. Save the `.md` report to the output destination:
   - **WORKSPACE mode**: `<workspace>/office-agent/analysis.md`
   - **IN-PLACE mode**: write `analysis.md` to the **same directory** as the target file

**Critical**: The final output file MUST be `analysis.md` (Markdown), not `analysis.json` or
any other format. Structured JSON data may be written as workspace scratch files but must NOT
be the only deliverable.

### Step 3c: Folder Organization (office.folder.organize)

**Goal**: Reorganize files into a logical structure. When the user specifies criteria, follow them
exactly. When the user does NOT specify, autonomously determine the best organization strategy by
scanning file contents and metadata.

#### 3c-1: Strategy Selection

1. Call `report_progress` with:
   `"Scanning folder structure: inventorying all files to determine best organization strategy"`
2. Use `list_local_dir` and `search_local_files` to enumerate all files recursively.
3. **If the user specified organization criteria** (e.g. "by student name", "by date", "by topic"):
   - Use exactly the user's criteria — do not override it.
   - Proceed directly to Step 3c-2 with the user's grouping rule.
4. **If the user did NOT specify criteria** — choose the best strategy autonomously:
   - Read a sample of file contents (first 20–50 lines each) to detect patterns.
   - Consider these strategies (pick the one that produces the most meaningful groupings):
     - **By author/person**: detect names in headers, bylines, or metadata
     - **By date/period**: detect dates in filenames or file content
     - **By topic/theme**: detect subject matter from document titles or first paragraphs
     - **By file type**: group `.pdf`, `.docx`, `.csv`, etc.
     - **By project/department**: detect project names or organizational units
   - Choose the strategy that creates ≥2 non-trivial groups with clear, distinct names.
   - Document your chosen strategy and rationale in `organization-plan.json`.
5. Call `report_progress` with:
   `"Strategy selected: organizing by <strategy>. Identified <N> groups: <group names>"`

#### 3c-2: Execute Organization

1. Write the plan to `<workspace>/office-agent/organization-plan.json`:
   ```json
   {"strategy": "<chosen strategy>", "rationale": "...",
    "groups": [{"name": "...", "files": [...]}]}
   ```
2. Create the organized output:
   - **WORKSPACE mode** (source bind is read-only):
     - Create `<workspace>/office-agent/organized/<group-name>/` directories.
     - For each file: read its content and write it to `organized/<group-name>/<filename>`.
     - Call `report_progress` with:
       `"Copying files to organized output: <N> files into <M> group directories"`
   - **IN-PLACE mode** (source bind is read-write):
     - Create subdirectories inside the source root using `run_local_command` (`mkdir -p`).
     - **Efficiency**: batch-read all files in ONE command to avoid exhausting the turn budget:
       ```
       find <target_dir> -type f | sort | xargs -I{} sh -c 'printf "=== {} ===\n"; head -10 "{}"'
       ```
     - Determine the group name for every file, then batch-move using `mv` commands.
     - Do NOT read files one-by-one with `read_local_file` for organize tasks.
     - Do NOT infer groups from existing subdirectory names.
     - Call `report_progress` with:
       `"Reorganizing files in-place: moving <N> files into <M> subdirectories"`
3. Write `<workspace>/office-agent/organization-report.md` summarizing:
   - Strategy used and why
   - Each group: name, file count, list of files
   - Files that could not be classified (placed in a default/other group)
4. Call `report_progress` with:
   `"Organization complete: <N> files reorganized into <M> groups using <strategy> strategy"`

### Step 4: Completion

1. Write `<workspace>/office-agent/warnings.md` if any files were skipped.
2. Call `collect_task_evidence` to capture output paths.
3. Call `check_definition_of_done` with an appropriate checklist.
4. Call `report_progress` with:
   `"Validating outputs: confirming all expected files are present and readable"`
5. Verify outputs by calling `read_local_file` or `list_local_dir` on the output path.
6. Call `complete_current_task` with:
   - A concise user-facing summary of what was accomplished
   - The full output file path(s) as artifacts
   - Any warnings about skipped or unreadable files

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
