"""Office agent LLM-facing tools: document reading, CSV analysis, workspace write."""

from __future__ import annotations

import csv
import json
import os
import shutil
import time
from pathlib import Path

from framework.tools.base import BaseTool, ToolResult


# ---------------------------------------------------------------------------
# Path validation helper
# ---------------------------------------------------------------------------

def _get_source_root() -> str:
    return os.environ.get("OFFICE_SOURCE_ROOT", "/")


def _validate_path(path: str) -> tuple[str, str]:
    """Validate that path is within OFFICE_SOURCE_ROOT and OFFICE_ALLOWED_BASE_PATHS.

    Returns (normalized_path, "") on success.
    Returns ("", error_message) on failure.
    """
    source_root = os.environ.get("OFFICE_SOURCE_ROOT", "/")
    allowed_bases = os.environ.get("OFFICE_ALLOWED_BASE_PATHS", "")

    try:
        real_path = os.path.realpath(os.path.abspath(path))
        real_root = os.path.realpath(os.path.abspath(source_root))

        # If OFFICE_SOURCE_ROOT is explicitly set to "/" (root), allow all paths
        if real_root != os.sep:
            prefix = real_root.rstrip(os.sep) + os.sep
            if os.path.islink(path):
                link_target = os.path.realpath(os.path.abspath(os.readlink(path)))
                if not link_target.startswith(prefix):
                    return "", "Symlink target outside OFFICE_SOURCE_ROOT"
            # Allow exact match (accessing the source root directory itself)
            if real_path != real_root and not real_path.startswith(prefix):
                return "", f"Path {path!r} is outside OFFICE_SOURCE_ROOT"

        # Check OFFICE_ALLOWED_BASE_PATHS whitelist if set
        if allowed_bases:
            allowed_list = [bp.strip() for bp in allowed_bases.split(":") if bp.strip()]
            if allowed_list:
                in_allowed = False
                for base in allowed_list:
                    base_real = os.path.realpath(os.path.abspath(base))
                    if real_path.startswith(base_real + os.sep) or real_path == base_real:
                        in_allowed = True
                        break
                if not in_allowed:
                    return "", f"Path {path!r} is not in OFFICE_ALLOWED_BASE_PATHS. Allowed bases: {allowed_bases}"

        return real_path, ""
    except Exception as exc:
        return "", f"Path validation error: {exc}"


# ---------------------------------------------------------------------------
# File size limit helper
# ---------------------------------------------------------------------------

def _check_file_size(path: str) -> tuple[bool, str]:
    """Check if file exceeds size limit. Returns (ok, error_message)."""
    max_size_mb = int(os.environ.get("OFFICE_MAX_FILE_SIZE_MB", "50"))
    max_size_bytes = max_size_mb * 1024 * 1024
    try:
        size = os.path.getsize(path)
        if size > max_size_bytes:
            return False, f"File size {size / (1024*1024):.1f}MB exceeds maximum allowed size of {max_size_mb}MB. Set OFFICE_MAX_FILE_SIZE_MB to increase limit."
        return True, ""
    except OSError:
        return True, ""  # Let the file read handle the error


# ---------------------------------------------------------------------------
# Audit file helpers
# ---------------------------------------------------------------------------

ORGANIZED_OUTPUT_ROOT = "organized-output/files/"
VALID_CATEGORIES = {"students", "documents", "data", "code", "images", "presentations"}
WRAPPER_PREFIXES = {"grouped", "by-student", "organized", "output", "originals"}


def _is_wrapper_prefixed(path: str) -> bool:
    """Check if path starts with a wrapper prefix like grouped/, by-student/, etc."""
    parts = path.strip("/").split("/")
    if parts:
        return parts[0].rstrip("s") in WRAPPER_PREFIXES or parts[0] in WRAPPER_PREFIXES
    return False


def _normalize_organized_path(target_path: str, output_mode: str = "workspace") -> str:
    """Normalize organize output path to organized-output/files/ schema.

    Strips wrapper prefixes like grouped/, by-student/, etc. and ensures
    all paths are under organized-output/files/.
    """
    path = target_path.strip()
    # Strip leading slash
    if path.startswith("/"):
        path = path[1:]
    # Strip wrapper prefixes
    while _is_wrapper_prefixed(path):
        parts = path.strip("/").split("/", 1)
        path = parts[1] if len(parts) > 1 else ""
    # Add schema prefix
    if not path.startswith(ORGANIZED_OUTPUT_ROOT):
        path = ORGANIZED_OUTPUT_ROOT + path
    return path


def _is_under_organized_output(path: str) -> bool:
    """Check if path is under organized-output/files/ schema."""
    normalized = path.strip()
    if normalized.startswith("/"):
        normalized = normalized[len(ORGANIZED_OUTPUT_ROOT):]
    # Check if it's under organized-output/files/ or is a category-relative path
    if normalized.startswith(ORGANIZED_OUTPUT_ROOT) or normalized == ORGANIZED_OUTPUT_ROOT.rstrip("/"):
        return True
    # Check if it's a category-relative path (students/, documents/, data/, etc.)
    first_component = normalized.split("/")[0] if normalized else ""
    return first_component in VALID_CATEGORIES


def _get_audit_dir() -> str:
    """Return the audit directory path for organize operations."""
    audit_dir = os.environ.get("OFFICE_AUDIT_DIR", "")
    if audit_dir:
        return audit_dir
    workspace_root = os.environ.get("OFFICE_WORKSPACE_ROOT", "")
    if workspace_root:
        return workspace_root
    artifact_root = os.environ.get("ARTIFACT_ROOT", "/tmp")
    return os.path.join(artifact_root, "office", "audit")


def _backup_existing_file(path: str) -> str | None:
    """Backup existing file with timestamp suffix. Returns backup path or None."""
    backup_enabled = os.environ.get("OFFICE_BACKUP_ENABLED", "true").lower()
    if backup_enabled not in ("true", "1", "yes"):
        return None
    if not os.path.exists(path):
        return None
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = f"{path}.{timestamp}.bak"
    try:
        os.rename(path, backup_path)
        return backup_path
    except OSError:
        return None


def _write_operations_plan(steps: list[dict], source_paths: list[str], output_mode: str, task_id: str = "") -> str:
    """Write operations-plan.json before any file operation begins."""
    audit_dir = _get_audit_dir()
    os.makedirs(audit_dir, exist_ok=True)
    plan_path = os.path.join(audit_dir, "operations-plan.json")
    plan = {
        "version": "1.0",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "capability": "organize",
        "task_id": task_id,
        "source_paths": source_paths,
        "output_mode": output_mode,
        "steps": [{"step_id": i + 1, "status": "pending", **s} for i, s in enumerate(steps)],
    }
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    return plan_path


def _update_step_status(step_id: int, status: str, plan_path: str | None = None) -> None:
    """Update a step's status in operations-plan.json."""
    if not plan_path or not os.path.exists(plan_path):
        return
    try:
        with open(plan_path, encoding="utf-8") as f:
            plan = json.load(f)
        for step in plan.get("steps", []):
            if step.get("step_id") == step_id:
                step["status"] = status
                break
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _append_command_log(action: str, params: dict, step_id: int | None = None) -> None:
    """Append a timestamped operation entry to command-log.txt."""
    audit_dir = _get_audit_dir()
    os.makedirs(audit_dir, exist_ok=True)
    log_path = os.path.join(audit_dir, "command-log.txt")
    entry = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] STEP {step_id or '?'}: {action} {json.dumps(params, ensure_ascii=False)}\n"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass


def _write_stage_summary(stage: str, completed_steps: list, pending_steps: list, warnings: list, errors: list) -> str:
    """Write stage-summary.json after each major stage."""
    audit_dir = _get_audit_dir()
    os.makedirs(audit_dir, exist_ok=True)
    summary_path = os.path.join(audit_dir, "stage-summary.json")
    summary = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "stage": stage,
        "completed_steps": completed_steps,
        "pending_steps": pending_steps,
        "warnings": warnings,
        "errors": errors,
    }
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary_path


def _write_manifest_entry(action: str, target: str, status: str, task_id: str = "") -> str | None:
    """Write/update .office-agent-manifest.json incrementally for in-place mode."""
    audit_dir = os.environ.get("OFFICE_SOURCE_ROOT", "")
    if not audit_dir:
        return None
    manifest_path = os.path.join(audit_dir, ".office-agent-manifest.json")
    manifest = {"version": "1.0", "task_id": task_id, "output_mode": "inplace", "operations": []}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            pass
    manifest["operations"].append({"action": action, "target": target, "status": status, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    manifest["rollback_possible"] = True
    try:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        return manifest_path
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Directory resource pre-check
# ---------------------------------------------------------------------------

def _scan_directory_resources(path: str) -> tuple[int, int]:
    """Scan directory tree and return (file_count, total_bytes).

    Skips files > 50MB and hidden files/directories.
    """
    file_count = 0
    total_bytes = 0
    max_file_size = 50 * 1024 * 1024  # 50MB
    try:
        for root, dirs, files in os.walk(path):
            # Skip hidden directories
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in files:
                if fname.startswith("."):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    size = os.path.getsize(fpath)
                    if size > max_file_size:
                        continue  # Skip oversized files
                    file_count += 1
                    total_bytes += size
                except OSError:
                    continue
    except OSError:
        pass
    return file_count, total_bytes


def _check_directory_limits(path: str) -> dict | None:
    """Check if directory exceeds resource limits.

    Returns error dict if exceeded, None if OK.
    """
    max_files = int(os.environ.get("OFFICE_MAX_TOTAL_FILES", "1000"))
    max_bytes = int(os.environ.get("OFFICE_MAX_TOTAL_BYTES", str(500 * 1024 * 1024)))
    file_count, total_bytes = _scan_directory_resources(path)
    if file_count > max_files:
        return {
            "error": "Resource limit exceeded",
            "pre_check_report": {
                "total_files": file_count,
                "max_files": max_files,
                "total_bytes": total_bytes,
                "max_bytes": max_bytes,
                "limit_type": "file_count",
            },
        }
    if total_bytes > max_bytes:
        return {
            "error": "Resource limit exceeded",
            "pre_check_report": {
                "total_files": file_count,
                "max_files": max_files,
                "total_bytes": total_bytes,
                "max_bytes": max_bytes,
                "limit_type": "total_bytes",
            },
        }
    return None


# ---------------------------------------------------------------------------
# Read PDF Tool
# ---------------------------------------------------------------------------

class ReadPdfTool(BaseTool):
    name = "read_pdf"
    description = "Read the full text content of a PDF file. Returns the text content."
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the PDF file"},
        },
        "required": ["path"],
    }

    def execute_sync(self, path: str = "") -> ToolResult:
        normalized, err = _validate_path(path)
        if err:
            return ToolResult(output="", error=f"read_pdf: {err}")
        if not os.path.isfile(normalized):
            return ToolResult(output="", error=f"read_pdf: file not found: {path}")
        if not normalized.lower().endswith(".pdf"):
            return ToolResult(output="", error=f"read_pdf: not a PDF file: {path}")
        ok, size_err = _check_file_size(normalized)
        if not ok:
            return ToolResult(output="", error=f"read_pdf: {size_err}")
        try:
            import pdfplumber
            with pdfplumber.open(normalized) as pdf:
                pages = []
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    if text.strip():
                        pages.append(f"[Page {page.page_number}]\n{text}")
                content = "\n\n".join(pages)
            size_kb = os.path.getsize(normalized) // 1024
            return ToolResult(output=json.dumps({
                "content": content,
                "path": normalized,
                "pages": len(pages),
                "size_kb": size_kb,
            }))
        except Exception as exc:
            return ToolResult(output="", error=f"read_pdf: failed to read: {exc}")


# ---------------------------------------------------------------------------
# Read DOCX Tool
# ---------------------------------------------------------------------------

class ReadDocxTool(BaseTool):
    name = "read_docx"
    description = "Read the full text content of a DOCX file. Returns the text content including tables."
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the DOCX file"},
        },
        "required": ["path"],
    }

    def execute_sync(self, path: str = "") -> ToolResult:
        normalized, err = _validate_path(path)
        if err:
            return ToolResult(output="", error=f"read_docx: {err}")
        if not os.path.isfile(normalized):
            return ToolResult(output="", error=f"read_docx: file not found: {path}")
        if normalized.lower().endswith(".doc"):
            return ToolResult(output="", error="read_docx: .doc format is not supported. Please convert to .docx first.")
        if not normalized.lower().endswith(".docx"):
            return ToolResult(output="", error=f"read_docx: not a DOCX file: {path}")
        ok, size_err = _check_file_size(normalized)
        if not ok:
            return ToolResult(output="", error=f"read_docx: {size_err}")
        try:
            import docx
            doc = docx.Document(normalized)
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            content = "\n\n".join(paragraphs)
            return ToolResult(output=json.dumps({
                "content": content,
                "path": normalized,
                "paragraphs": len(paragraphs),
            }))
        except Exception as exc:
            return ToolResult(output="", error=f"read_docx: failed to read: {exc}")


# ---------------------------------------------------------------------------
# Read PPTX Tool
# ---------------------------------------------------------------------------

class ReadPptxTool(BaseTool):
    name = "read_pptx"
    description = "Read the full text content of a PowerPoint PPTX file. Extracts text from all slides."
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the PPTX file"},
        },
        "required": ["path"],
    }

    def execute_sync(self, path: str = "") -> ToolResult:
        normalized, err = _validate_path(path)
        if err:
            return ToolResult(output="", error=f"read_pptx: {err}")
        if not os.path.isfile(normalized):
            return ToolResult(output="", error=f"read_pptx: file not found: {path}")
        if not normalized.lower().endswith(".pptx"):
            return ToolResult(output="", error="read_pptx: only .pptx files are supported. Please convert .ppt to .pptx first.")
        ok, size_err = _check_file_size(normalized)
        if not ok:
            return ToolResult(output="", error=f"read_pptx: {size_err}")
        try:
            import pptx
            prs = pptx.Presentation(normalized)
            slides = []
            for i, slide in enumerate(prs.slides):
                slide_text = []
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        slide_text.append(shape.text.strip())
                if slide_text:
                    slides.append(f"[Slide {i+1}]\n" + "\n".join(slide_text))
            content = "\n\n".join(slides)
            return ToolResult(output=json.dumps({
                "content": content,
                "path": normalized,
                "slides": len(slides),
                "total_slides": len(prs.slides),
            }))
        except Exception as exc:
            return ToolResult(output="", error=f"read_pptx: failed to read: {exc}")


# ---------------------------------------------------------------------------
# Read TXT Tool
# ---------------------------------------------------------------------------

class ReadTxtTool(BaseTool):
    name = "read_txt"
    description = "Read the full text content of a plain text file."
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the TXT file"},
        },
        "required": ["path"],
    }

    def execute_sync(self, path: str = "") -> ToolResult:
        normalized, err = _validate_path(path)
        if err:
            return ToolResult(output="", error=f"read_txt: {err}")
        if not os.path.isfile(normalized):
            return ToolResult(output="", error=f"read_txt: file not found: {path}")
        ok, size_err = _check_file_size(normalized)
        if not ok:
            return ToolResult(output="", error=f"read_txt: {size_err}")
        try:
            import chardet
            with open(normalized, "rb") as fh:
                raw = fh.read()
            detected = chardet.detect(raw)
            encoding = detected.get("encoding", "utf-8") or "utf-8"
            content = raw.decode(encoding, errors="replace")
            return ToolResult(output=json.dumps({
                "content": content,
                "path": normalized,
                "chars": len(content),
                "encoding": encoding,
            }))
        except Exception as exc:
            return ToolResult(output="", error=f"read_txt: failed to read: {exc}")


# ---------------------------------------------------------------------------
# Read CSV Tool
# ---------------------------------------------------------------------------

class ReadCsvTool(BaseTool):
    name = "read_csv"
    description = "Read a CSV file and return headers and rows as structured data. Use for data analysis tasks."
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the CSV file"},
        },
        "required": ["path"],
    }

    def execute_sync(self, path: str = "") -> ToolResult:
        normalized, err = _validate_path(path)
        if err:
            return ToolResult(output="", error=f"read_csv: {err}")
        if not os.path.isfile(normalized):
            return ToolResult(output="", error=f"read_csv: file not found: {path}")
        if not normalized.lower().endswith(".csv"):
            return ToolResult(output="", error=f"read_csv: not a CSV file: {path}")
        ok, size_err = _check_file_size(normalized)
        if not ok:
            return ToolResult(output="", error=f"read_csv: {size_err}")
        try:
            import chardet
            with open(normalized, "rb") as fh:
                raw = fh.read()
            detected = chardet.detect(raw)
            encoding = detected.get("encoding", "utf-8") or "utf-8"
            text = raw.decode(encoding, errors="replace")
            reader = csv.reader(text.splitlines())
            rows = list(reader)
            if not rows:
                return ToolResult(output="", error="read_csv: CSV file is empty")
            headers = rows[0]
            data_rows = rows[1:]
            stats = {}
            for col_idx, header in enumerate(headers):
                try:
                    vals = [float(row[col_idx]) for row in data_rows if col_idx < len(row) and row[col_idx].strip()]
                    if vals:
                        stats[header] = {
                            "count": len(vals),
                            "min": min(vals),
                            "max": max(vals),
                            "avg": round(sum(vals) / len(vals), 2),
                        }
                except (ValueError, IndexError):
                    pass
            return ToolResult(output=json.dumps({
                "headers": headers,
                "rows": data_rows[:1000],
                "total_rows": len(data_rows),
                "stats": stats,
                "encoding": encoding,
            }))
        except Exception as exc:
            return ToolResult(output="", error=f"read_csv: failed to read: {exc}")


# ---------------------------------------------------------------------------
# Read XLSX Tool
# ---------------------------------------------------------------------------

class ReadXlsxTool(BaseTool):
    name = "read_xlsx"
    description = "Read the full content of an Excel XLSX file. Returns structured data with sheet names, headers, and rows."
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the XLSX file"},
        },
        "required": ["path"],
    }

    def execute_sync(self, path: str = "") -> ToolResult:
        normalized, err = _validate_path(path)
        if err:
            return ToolResult(output="", error=f"read_xlsx: {err}")
        if not os.path.isfile(normalized):
            return ToolResult(output="", error=f"read_xlsx: file not found: {path}")
        if not normalized.lower().endswith((".xlsx", ".xlsm")):
            return ToolResult(output="", error=f"read_xlsx: not an XLSX file: {path}")
        ok, size_err = _check_file_size(normalized)
        if not ok:
            return ToolResult(output="", error=f"read_xlsx: {size_err}")
        try:
            import openpyxl
            wb = openpyxl.load_workbook(normalized, read_only=True, data_only=True)
            sheets = {}
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                rows = []
                for row in ws.iter_rows(values_only=True):
                    rows.append(list(row))
                sheets[sheet_name] = {
                    "rows": rows,
                    "row_count": len(rows),
                }
            return ToolResult(output=json.dumps({
                "path": normalized,
                "sheets": sheets,
                "sheet_names": wb.sheetnames,
            }))
        except Exception as exc:
            return ToolResult(output="", error=f"read_xlsx: failed to read: {exc}")


# ---------------------------------------------------------------------------
# Read XLS Tool
# ---------------------------------------------------------------------------

class ReadXlsTool(BaseTool):
    name = "read_xls"
    description = "Read the content of a legacy Excel XLS file. Best-effort support — convert to .xlsx for better results."
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the XLS file"},
        },
        "required": ["path"],
    }

    def execute_sync(self, path: str = "") -> ToolResult:
        normalized, err = _validate_path(path)
        if err:
            return ToolResult(output="", error=f"read_xls: {err}")
        if not os.path.isfile(normalized):
            return ToolResult(output="", error=f"read_xls: file not found: {path}")
        if not normalized.lower().endswith(".xls"):
            return ToolResult(output="", error=f"read_xls: not an XLS file: {path}")
        ok, size_err = _check_file_size(normalized)
        if not ok:
            return ToolResult(output="", error=f"read_xls: {size_err}")
        try:
            import xlrd
            wb = xlrd.open_workbook(normalized)
            sheets = {}
            for i in range(wb.nsheets):
                ws = wb.sheet_by_index(i)
                rows = []
                for row_idx in range(ws.nrows):
                    rows.append(ws.row_values(row_idx))
                sheets[ws.name] = {
                    "rows": rows,
                    "row_count": ws.nrows,
                }
            return ToolResult(output=json.dumps({
                "path": normalized,
                "sheets": sheets,
                "sheet_names": wb.sheet_names(),
                "warning": "XLS format has limited support. Convert to XLSX for better results.",
            }))
        except ImportError:
            return ToolResult(output="", error="read_xls: xlrd library not installed. Convert the XLS file to XLSX format.")
        except Exception as exc:
            return ToolResult(output="", error=f"read_xls: failed to read (best-effort): {exc}")


# ---------------------------------------------------------------------------
# List Directory Tool
# ---------------------------------------------------------------------------

class ListDirectoryTool(BaseTool):
    name = "list_directory"
    description = "List the files and subdirectories in a given path, with file sizes and types."
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the directory"},
        },
        "required": ["path"],
    }

    def execute_sync(self, path: str = "") -> ToolResult:
        normalized, err = _validate_path(path)
        if err:
            return ToolResult(output="", error=f"list_directory: {err}")
        if not os.path.isdir(normalized):
            return ToolResult(output="", error=f"list_directory: not a directory: {path}")
        try:
            entries = []
            for name in os.listdir(normalized):
                full = os.path.join(normalized, name)
                try:
                    stat = os.stat(full)
                    entries.append({
                        "name": name,
                        "type": "dir" if os.path.isdir(full) else "file",
                        "size": stat.st_size if os.path.isfile(full) else 0,
                    })
                except OSError:
                    pass
            return ToolResult(output=json.dumps({"files": entries}))
        except Exception as exc:
            return ToolResult(output="", error=f"list_directory: failed: {exc}")


# ---------------------------------------------------------------------------
# Write Workspace Tool
# ---------------------------------------------------------------------------

class WriteWorkspaceTool(BaseTool):
    name = "write_workspace"
    description = "Write content to a file in the workspace artifacts folder. Use this for workspace output mode."
    parameters_schema = {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "Output filename (e.g., summary.md, analysis.md)"},
            "content": {"type": "string", "description": "File content to write"},
        },
        "required": ["filename", "content"],
    }

    def execute_sync(self, filename: str = "", content: str = "") -> ToolResult:
        workspace_root = os.environ.get(
            "OFFICE_WORKSPACE_ROOT",
            os.path.join(os.environ.get("ARTIFACT_ROOT", "/tmp"), "office")
        )
        try:
            os.makedirs(workspace_root, exist_ok=True)
            safe_name = os.path.basename(filename)
            if not safe_name or safe_name != filename:
                return ToolResult(output="", error=f"write_workspace: invalid filename {filename!r}")
            out_path = os.path.join(workspace_root, safe_name)
            backup_path = _backup_existing_file(out_path)
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            result = {"path": out_path, "bytes": len(content.encode("utf-8"))}
            if backup_path:
                result["backup_path"] = backup_path
            return ToolResult(output=json.dumps(result))
        except Exception as exc:
            return ToolResult(output="", error=f"write_workspace: failed: {exc}")


# ---------------------------------------------------------------------------
# Write File Tool (inplace mode — requires write grant)
# ---------------------------------------------------------------------------

class WriteFileTool(BaseTool):
    name = "write_file"
    description = "Write content to a file at the specified path. Only available when inplace mode is enabled with write grant."
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to write to (must be under OFFICE_SOURCE_ROOT)"},
            "content": {"type": "string", "description": "File content to write"},
        },
        "required": ["path", "content"],
    }

    def execute_sync(self, path: str = "", content: str = "") -> ToolResult:
        allow_inplace = os.environ.get("OFFICE_ALLOW_INPLACE_WRITES", "false").lower()
        if allow_inplace not in ("true", "1", "yes"):
            return ToolResult(output="", error="write_file: inplace mode not enabled. Set OFFICE_ALLOW_INPLACE_WRITES=true")
        normalized, err = _validate_path(path)
        if err:
            return ToolResult(output="", error=f"write_file: {err}")
        backup_path = _backup_existing_file(normalized)
        try:
            os.makedirs(os.path.dirname(normalized), exist_ok=True)
            with open(normalized, "w", encoding="utf-8") as fh:
                fh.write(content)
            result = {"path": normalized, "bytes": len(content.encode("utf-8"))}
            if backup_path:
                result["backup_path"] = backup_path
            return ToolResult(output=json.dumps(result))
        except Exception as exc:
            return ToolResult(output="", error=f"write_file: failed: {exc}")


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
            errors = []
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
                    errors.append(name)

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
                for f in sorted(files, key=str.lower):
                    plan_lines.append(f"- `{f}`")
                plan_lines.append("")

            plan_content = "\n".join(plan_lines)

            return ToolResult(output=json.dumps({
                "path": normalized,
                "groups": groups,
                "plan_content": plan_content,
                "total_files": len([e for e in entries if e["type"] == "file"]),
                "total_dirs": len([e for e in entries if e["type"] == "dir"]),
                "errors": errors,
            }))
        except Exception as exc:
            return ToolResult(output="", error=f"organize_folder: failed: {exc}")


class OrganizeMoveFileTool(BaseTool):
    name = "organize_move_file"
    description = """Execute a planned file move as part of folder organization.
Before executing, validates: (1) action is in whitelist (mkdir, copy_file, write_text),
(2) all paths are within OFFICE_SOURCE_ROOT, (3) plan is written to operations-plan.json first.
Use organize_folder tool first to survey the folder, then organize_execute_plan to run."""
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "Action type: mkdir, copy_file, or write_text"},
            "src": {"type": "string", "description": "Source path (for copy_file)"},
            "dst": {"type": "string", "description": "Destination path"},
            "content": {"type": "string", "description": "File content (for write_text action)"},
        },
        "required": ["action", "dst"],
    }

    ALLOWED_ACTIONS = {"mkdir", "copy_file", "write_text"}

    def execute_sync(self, action: str = "", src: str = "", dst: str = "", content: str = "") -> ToolResult:
        # Check if inplace writes are allowed
        allow_inplace = os.environ.get("OFFICE_ALLOW_INPLACE_WRITES", "false").lower()
        if allow_inplace not in ("true", "1", "yes"):
            return ToolResult(output="", error="organize_move_file: inplace writes not enabled. Set OFFICE_ALLOW_INPLACE_WRITES=true")

        # Validate action is in whitelist
        if action not in self.ALLOWED_ACTIONS:
            return ToolResult(output="", error=f"organize_move_file: action {action!r} not allowed. Allowed: {self.ALLOWED_ACTIONS}")

        # Validate destination path
        dst_normalized, err = _validate_path(dst)
        if err:
            return ToolResult(output="", error=f"organize_move_file: destination {err}")

        # Reject wrapper prefixes (grouped/, by-student/, output/, organized/)
        if _is_wrapper_prefixed(dst):
            return ToolResult(output="", error=f"organize_move_file: destination {dst!r} is outside the organized-output/files/ schema (wrapper prefix not allowed)")

        # Validate source path if provided
        if src:
            src_normalized, err = _validate_path(src)
            if err:
                return ToolResult(output="", error=f"organize_move_file: source {err}")
        else:
            src_normalized = ""

        # Write operations-plan.json before executing
        workspace_root = os.environ.get("OFFICE_WORKSPACE_ROOT", "")
        if workspace_root:
            plan_path = os.path.join(workspace_root, "operations-plan.json")
            with open(plan_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "action": action,
                    "src": src_normalized,
                    "dst": dst_normalized,
                    "content_length": len(content) if content else 0,
                }) + "\n")

        # Execute action
        try:
            if action == "mkdir":
                os.makedirs(dst_normalized, exist_ok=True)
                return ToolResult(output=json.dumps({"path": dst_normalized, "action": "mkdir"}))
            elif action == "copy_file":
                if not src_normalized:
                    return ToolResult(output="", error="organize_move_file: copy_file requires src")
                shutil.copy2(src_normalized, dst_normalized)
                return ToolResult(output=json.dumps({"from": src_normalized, "to": dst_normalized, "action": "copy_file"}))
            elif action == "write_text":
                if not content:
                    return ToolResult(output="", error="organize_move_file: write_text requires content")
                os.makedirs(os.path.dirname(dst_normalized), exist_ok=True)
                with open(dst_normalized, "w", encoding="utf-8") as f:
                    f.write(content)
                return ToolResult(output=json.dumps({"path": dst_normalized, "action": "write_text", "bytes": len(content)}))
        except Exception as exc:
            return ToolResult(output="", error=f"organize_move_file: {exc}")


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

_OFFICE_TOOLS = [
    ReadPdfTool(),
    ReadDocxTool(),
    ReadPptxTool(),
    ReadTxtTool(),
    ReadCsvTool(),
    ReadXlsxTool(),
    ReadXlsTool(),
    ListDirectoryTool(),
    WriteWorkspaceTool(),
    WriteFileTool(),
    OrganizeFolderTool(),
    OrganizeMoveFileTool(),
]


def register_office_tools() -> None:
    """Register office tools into the global ToolRegistry (idempotent)."""
    from framework.tools.registry import get_registry
    registry = get_registry()
    for tool in _OFFICE_TOOLS:
        registry.register(tool)