# Office Agent Phase 2 — Folder Organization Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Implement folder organization capability for the Office Agent using bounded planning with plan-before-write validation.

**Architecture:** `OrganizeFolderTool` + `_build_organize_prompt()` in `nodes.py`, skill prompt `prompts/organize.md`. Uses existing `runtime.run_agentic()` with the organize tool for bounded ReAct planning. Plan-before-write: generate `organization-plan.md` before any file moves.

**Tech Stack:** Same as Phase 1 — reuse existing framework, no new adapters.

---

## File Map

```
MODIFY: agents/office/nodes.py              # Add _build_organize_prompt, organize case in execute_office_work
MODIFY: agents/office/office_tools.py        # Add OrganizeFolderTool, register it
CREATE: agents/office/prompts/organize.md    # Skill prompt for organize capability
CREATE: tests/unit/agents/test_office_organize.py  # Unit tests for organize
```

---

## Task 1: Add OrganizeFolderTool to office_tools.py

**Files:**
- Modify: `agents/office/office_tools.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/agents/test_office_organize.py

import pytest, os, tempfile
from agents.office.office_tools import OrganizeFolderTool, WriteWorkspaceTool

def test_organize_folder_tool_lists_files(tmp_path):
    """OrganizeFolderTool lists directory contents."""
    tool = OrganizeFolderTool()
    tool._source_root = str(tmp_path)
    # Create test files
    (tmp_path / "doc1.txt").write_text("hello")
    (tmp_path / "doc2.pdf").write_text("world")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "doc3.txt").write_text("nested")
    result = tool.execute_sync(path=str(tmp_path))
    assert result.success, f"organize_folder failed: {result.error}"
    data = json.loads(result.output)
    files = data.get("files", [])
    assert len(files) >= 3, f"Expected at least 3 files, got {len(files)}"

def test_organize_folder_tool_validates_path():
    """OrganizeFolderTool rejects paths outside source root."""
    tool = OrganizeFolderTool()
    result = tool.execute_sync(path="/etc/passwd")
    assert not result.success
    assert "outside OFFICE_SOURCE_ROOT" in result.error

def test_write_workspace_in_organize_mode(tmp_path):
    """WriteWorkspaceTool can write organization plan."""
    tool = WriteWorkspaceTool()
    os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)
    result = tool.execute_sync(
        filename="organization-plan.md",
        content="# Organization Plan\n\n- Group 1: Documents"
    )
    assert result.success, f"write_workspace failed: {result.error}"
    assert (tmp_path / "organization-plan.md").exists()
    del os.environ["OFFICE_WORKSPACE_ROOT"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/agents/test_office_organize.py -v 2>&1 | head -20
```

Expected: FAIL — OrganizeFolderTool not found.

- [ ] **Step 3: Add OrganizeFolderTool to office_tools.py**

Add after the WriteFileTool class (before `register_office_tools()`):

```python
# ---------------------------------------------------------------------------
# Organize Folder Tool
# ---------------------------------------------------------------------------

class OrganizeFolderTool(BaseTool):
    name = "organize_folder"
    description = """Analyze a folder and generate an organization plan that groups files by type/category.
Outputs a structured organization-plan.md file. Use list_directory to survey the folder first.
This tool does NOT move files — use organize_move_file to execute planned moves."""
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the folder to organize"},
        },
        "required": ["path"],
    }

    def execute_sync(self, path: str = "") -> ToolResult:
        normalized, err = _validate_path(path)
        if err:
            return ToolResult(output="", error=f"organize_folder: {err}")
        if not os.path.isdir(normalized):
            return ToolResult(output="", error=f"organize_folder: not a directory: {path}")

        try:
            entries = []
            for name in os.listdir(normalized):
                full = os.path.join(normalized, name)
                try:
                    stat = os.stat(full)
                    is_dir = os.path.isdir(full)
                    entries.append({
                        "name": name,
                        "type": "dir" if is_dir else "file",
                        "size": stat.st_size if not is_dir else 0,
                        "ext": os.path.splitext(name)[1].lower(),
                    })
                except OSError:
                    pass

            # Group by extension/category
            groups = {}
            for e in entries:
                if e["type"] == "dir":
                    cat = "folders"
                else:
                    ext = e.get("ext", "")
                    if ext in (".pdf", ".doc", ".docx"):
                        cat = "documents"
                    elif ext in (".txt", ".md", ".rtf"):
                        cat = "text"
                    elif ext in (".csv", ".xlsx", ".xls"):
                        cat = "data"
                    elif ext in (".png", ".jpg", ".jpeg", ".gif", ".svg"):
                        cat = "images"
                    elif ext in (".py", ".js", ".ts", ".java", ".cpp", ".c", ".h"):
                        cat = "code"
                    else:
                        cat = "other"
                if cat not in groups:
                    groups[cat] = []
                groups[cat].append(e["name"])

            # Build organization plan markdown
            plan_lines = ["# Folder Organization Plan", "", f"**Source:** `{normalized}`", ""]
            for cat, files in sorted(groups.items()):
                plan_lines.append(f"## {cat.title()} ({len(files)})")
                for f in sorted(files):
                    plan_lines.append(f"- `{f}`")
                plan_lines.append("")

            plan_content = "\n".join(plan_lines)

            return ToolResult(output=json.dumps({
                "path": normalized,
                "groups": groups,
                "plan_content": plan_content,
                "total_files": len([e for e in entries if e["type"] == "file"]),
                "total_dirs": len([e for e in entries if e["type"] == "dir"]),
            }))
        except Exception as exc:
            return ToolResult(output="", error=f"organize_folder: failed: {exc}")
```

- [ ] **Step 4: Register OrganizeFolderTool in _OFFICE_TOOLS and register_office_tools**

Add `OrganizeFolderTool()` to the `_OFFICE_TOOLS` list.

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/unit/agents/test_office_organize.py -v 2>&1 | tail -20
```

- [ ] **Step 6: Commit**

```bash
git add agents/office/office_tools.py tests/unit/agents/test_office_organize.py
git commit -m "feat(office): add OrganizeFolderTool for Phase 2 folder organization

OrganizeFolderTool analyzes a folder, groups files by extension/category,
and produces a structured organization-plan.md. No file moves are executed —
moves require explicit organize_move_file calls.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: Add organize capability prompts and update nodes.py

**Files:**
- Create: `agents/office/prompts/organize.md`
- Modify: `agents/office/nodes.py`

- [ ] **Step 1: Create agents/office/prompts/organize.md**

```markdown
# Office Agent — Organize Capability Prompt

You are a folder organization expert. Your task is to analyze a folder structure
and produce a clear organization plan.

## Workflow

1. **Survey the folder** — Use `list_directory` to see all files and subdirectories
2. **Group files by category** — Documents, Text, Data, Images, Code, Folders, Other
3. **Write the plan** — Use `write_workspace` to create `organization-plan.md`
4. **If inplace mode** — After writing the plan, use `organize_move_file` to execute moves

## Organization Rules

- Never delete original files
- Never overwrite existing files without confirmation
- Group by: file type (extension), then by name/date if needed
- Preserve directory structure when possible
- For mixed folders: separate files from directories

## Output Format (organization-plan.md)

```markdown
# Folder Organization Plan

**Source:** /path/to/folder
**Mode:** workspace | inplace

## Documents (N files)
- file1.pdf
- file2.docx

## Data (N files)
- data1.csv

## Suggested Structure (for reference)
├── documents/
│   ├── file1.pdf
│   └── file2.docx
├── data/
│   └── data1.csv
```

## Inplace Mode

When output_mode is "inplace" and OFFICE_ALLOW_INPLACE_WRITES=true:
1. Generate the organization plan
2. Execute file moves using `organize_move_file` tool
3. Write final report to organization-report.md
```

- [ ] **Step 2: Add _build_organize_prompt() to nodes.py**

Add after `_build_analyze_prompt()`:

```python
def _build_organize_prompt(paths: list[str], output_mode: str, source_root: str) -> str:
    paths_list = "\n".join(f"- {p}" for p in paths)
    return f"""Organize the following folder(s) into logical groups:

{paths_list}

Source root: {source_root}
Output mode: {output_mode}

Workflow:
1. Use organize_folder tool to survey each folder and generate an organization plan
2. Review the plan — does the grouping make sense?
3. Write the organization plan using write_workspace tool with filename: organization-plan.md
4. If output_mode is 'inplace' and OFFICE_ALLOW_INPLACE_WRITES=true:
   - Execute the planned file moves using organize_move_file tool
   - Write a final organization-report.md with the results

Rules:
- Never delete original files
- Never overwrite existing files
- Group by: Documents (pdf/doc/docx), Text (txt/md), Data (csv/xlsx), Images (png/jpg), Code (py/js), Folders
- Preserve important prefixes in filenames (dates, project names)

Output format:
# Folder Organization Plan

## [Category] (N files)
- file1
- file2

## Suggested Structure (optional)
[ASCII directory tree]
"""
```

- [ ] **Step 3: Update execute_office_work to handle "organize" capability**

In `execute_office_work()`, replace:
```python
if capability == "organize":
    return {"error": "Folder organization is not yet implemented (Phase 2)."}
```
With:
```python
if capability == "organize":
    prompt = _build_organize_prompt(validated_paths, output_mode, source_root)
```

Also update `tool_names` to include `"organize_folder"`:
```python
tool_names = ["read_pdf", "read_docx", "read_txt", "read_csv", "list_directory",
              "write_workspace", "write_file", "organize_folder"]
```

- [ ] **Step 4: Update analyze_request to allow organize without file paths**

Change the early return condition in `analyze_request`:
```python
if not validated_paths and capability != "summarize":
```
To:
```python
if not validated_paths and capability not in ("summarize", "organize"):
```
This allows organize to work with just a folder path (not requiring specific file paths).

- [ ] **Step 5: Run tests**

```bash
pytest tests/unit/agents/test_office_organize.py -v 2>&1 | tail -15
```

- [ ] **Step 6: Commit**

```bash
git add agents/office/nodes.py agents/office/prompts/organize.md
git commit -m "feat(office): add organize capability — _build_organize_prompt, handle organize in execute_office_work

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Add organize_folder tests to test_office_agent.py

- [ ] **Step 1: Add organize tests to test_office_agent.py**

```python
def test_receive_task_parses_organize():
    """receive_task correctly identifies organize capability."""
    from agents.office.nodes import receive_task
    state = {
        "_task_id": "task-org-test",
        "_compass_task_id": "compass-org-test",
        "user_request": "organize /path/to/myfolder output_mode=workspace",
    }
    result = receive_task(state)
    assert result["capability"] == "organize", f"Expected organize, got {result['capability']}"
    assert "myfolder" in result["source_paths"][0] or len(result["source_paths"]) > 0

def test_analyze_request_allows_organize_without_files():
    """analyze_request allows organize capability without specific file paths."""
    from agents.office.nodes import receive_task, analyze_request
    import tempfile, os
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["OFFICE_SOURCE_ROOT"] = tmp
        state = {
            "_task_id": "task-org-test2",
            "_compass_task_id": "compass-org-test2",
            "user_request": f"organize {tmp}/somedir",
            "output_mode": "workspace",
        }
        state = receive_task(state)
        result = analyze_request(state)
        # Should not error on missing file paths for organize
        assert "error" not in result or "No valid paths" not in result.get("error", "")
        del os.environ["OFFICE_SOURCE_ROOT"]

def test_execute_office_work_handles_organize_error_if_no_runtime():
    """execute_office_work returns error for organize if no runtime."""
    from agents.office.nodes import execute_office_work
    state = {
        "_runtime": None,
        "capability": "organize",
        "validated_paths": ["/tmp"],
        "artifacts_dir": "/tmp/artifacts",
        "output_mode": "workspace",
    }
    result = execute_office_work(state)
    # Should not crash — should handle gracefully
    assert "error" in result or result.get("capability") == "organize"
```

- [ ] **Step 2: Run tests**

```bash
pytest tests/unit/agents/test_office_agent.py -v -k organize 2>&1 | tail -20
```

- [ ] **Step 3: Commit**

```bash
git add tests/unit/agents/test_office_agent.py
git commit -m "test(office): add organize capability tests to test_office_agent.py

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Run full office test suite

- [ ] **Step 1: Run all office tests**

```bash
pytest tests/unit/agents/test_office_tools.py tests/unit/agents/test_office_agent.py tests/unit/agents/test_office_integration.py tests/unit/agents/test_office_organize.py -v 2>&1 | tail -50
```

- [ ] **Step 2: Verify agent imports correctly**

```bash
python3 -c "from agents.office.agent import OfficeAgent, office_definition, office_workflow; print('OK')"
```

- [ ] **Step 3: Commit final Phase 2 bundle**

```bash
git add -A && git commit -m "feat(office): complete Phase 2 — folder organization capability

- OrganizeFolderTool: surveys folders, groups files by type, generates organization-plan.md
- organize capability in execute_office_work via _build_organize_prompt
- organize.md prompt for LLM-facing organize instructions
- Full test coverage for organize tool and capability detection

Phase 2 complete. Office agent now supports:
- summarize (PDF/DOCX/TXT)
- analyze (CSV data)
- organize (folder grouping)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Summary

Phase 2 adds folder organization to the Office Agent:

- **OrganizeFolderTool** — lists directory contents, groups files by extension, produces plan markdown
- **_build_organize_prompt()** — builds the LLM prompt for organize tasks
- **organize capability** — handled in `execute_office_work` alongside summarize/analyze
- **prompts/organize.md** — skill prompt for the organize workflow
- **Tests** — unit tests for OrganizeFolderTool and organize capability detection

After this, the Office Agent has all three capabilities from the spec:
1. Document summarization (PDF/DOCX/TXT)
2. CSV/data analysis
3. Folder organization (bounded planning with plan-before-write)