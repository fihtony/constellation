"""UI Design boundary tools — in-process implementations using UIDesignAgentAdapter.

Registered by UIDesignAgentAdapter.start() so the global ToolRegistry has live
design tools before Team Lead calls register_team_lead_tools().
"""
from __future__ import annotations

import json
import os

from pathlib import Path as _Path

from framework.config import load_agent_config as _load_agent_cfg
from framework.devlog import AgentLogger
from framework.tools.base import BaseTool, ToolResult
from framework.tools.registry import get_registry

# Load agent_id from config.yaml — single source of truth for identity
_AGENT_ID: str = _load_agent_cfg(
    _Path(__file__).parent.name.replace("_", "-")
).get("agent_id", _Path(__file__).parent.name.replace("_", "-"))


def _log(task_id: str) -> AgentLogger:
    return AgentLogger(task_id=task_id, agent_name=_AGENT_ID)


def _get_adapter():
    from agents.ui_design.adapter import UIDesignAgentAdapter, ui_design_definition
    from framework.agent import AgentServices
    from framework.checkpoint import InMemoryCheckpointer
    from framework.event_store import InMemoryEventStore
    from framework.memory import InMemoryMemoryService
    from framework.plugin import PluginManager
    from framework.runtime.adapter import get_runtime
    from framework.session import InMemorySessionService
    from framework.skills import SkillsRegistry
    from framework.task_store import InMemoryTaskStore

    services = AgentServices(
        session_service=InMemorySessionService(),
        event_store=InMemoryEventStore(),
        memory_service=InMemoryMemoryService(),
        skills_registry=SkillsRegistry(),
        plugin_manager=PluginManager(),
        checkpoint_service=InMemoryCheckpointer(),
        runtime=get_runtime(),
        registry_client=None,
        task_store=InMemoryTaskStore(),
    )
    return UIDesignAgentAdapter(definition=ui_design_definition, services=services)


class FetchDesign(BaseTool):
    name = "fetch_design"
    description = (
        "Fetch design specification from a Figma URL or a Google Stitch project. "
        "Provide either figma_url or stitch_project_id. "
        "Use capability='stitch.screen.image' to fetch screen image URL instead."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "figma_url": {"type": "string", "description": "Full Figma file URL."},
            "stitch_project_id": {"type": "string", "description": "Google Stitch project ID."},
            "stitch_screen_id": {"type": "string", "description": "Stitch screen ID (optional)."},
            "screen_name": {"type": "string", "description": "Screen name for Stitch (optional)."},
            "capability": {"type": "string", "description": "Override capability (e.g. stitch.screen.image)."},
            "task_id": {"type": "string", "description": "Caller task ID for log correlation (optional)."},
            "workspace_path": {"type": "string", "description": "Workspace root path. When provided, design files are saved to <workspace>/ui-design/[figma|stitch]/ folder."},
        },
        "required": [],
    }

    def execute_sync(
        self,
        figma_url: str = "",
        stitch_project_id: str = "",
        stitch_screen_id: str = "",
        screen_name: str = "",
        capability: str = "",
        task_id: str = "",
        workspace_path: str = "",
        **kw,
    ) -> ToolResult:
        log = _log(task_id)
        adapter = _get_adapter()
        if figma_url:
            log.info("fetch_design called", source="figma", figma_url=figma_url)
            result = adapter._dispatch(
                "figma.file.fetch", figma_url,
                {"metadata": {"figmaUrl": figma_url}},
            )
            if not result.get("error") and workspace_path:
                saved = _save_figma_files(result, workspace_path, log)
                result.update(saved)
        elif stitch_project_id:
            if capability:
                effective_capability = capability
            elif stitch_screen_id or screen_name:
                effective_capability = "stitch.screen.fetch"
            else:
                effective_capability = "stitch.screens.list"
            log.info("fetch_design called", source="stitch",
                     stitch_project_id=stitch_project_id, screen_id=stitch_screen_id,
                     capability=effective_capability,
                     workspace_path=workspace_path or "(not set)")
            result = adapter._dispatch(
                effective_capability, stitch_project_id,
                {
                    "metadata": {
                        "stitchProjectId": stitch_project_id,
                        "stitchScreenId": stitch_screen_id,
                        "screenName": screen_name,
                    }
                },
            )
            if not result.get("error") and workspace_path and effective_capability == "stitch.screen.fetch":
                # Also fetch project metadata for DESIGN.md (design tokens)
                project_result = adapter._dispatch(
                    "stitch.project.get", stitch_project_id,
                    {"metadata": {"stitchProjectId": stitch_project_id}},
                )
                saved = _save_stitch_files(
                    result, project_result, stitch_project_id, stitch_screen_id, workspace_path, log
                )
                result.update(saved)
                log.info("stitch design files saved",
                         local_folder=saved.get("local_folder", ""),
                         files=saved.get("files", []))
        else:
            # Fall back to env-configured sources
            eff_project_id = os.environ.get("STITCH_PROJECT_ID", "")
            eff_screen_id = os.environ.get("STITCH_SCREEN_ID", "")
            eff_figma_url = os.environ.get("FIGMA_FILE_URL", "")
            if eff_project_id:
                return self.execute_sync(
                    stitch_project_id=eff_project_id,
                    stitch_screen_id=eff_screen_id,
                    task_id=task_id,
                    workspace_path=workspace_path,
                )
            if eff_figma_url:
                return self.execute_sync(figma_url=eff_figma_url, task_id=task_id,
                                         workspace_path=workspace_path)
            log.warn("fetch_design: no design source configured")
            print("[ui-design-tools] No design source configured, returning empty context")
            return ToolResult(output=json.dumps({"design": {}, "status": "no_source"}))

        if result.get("error"):
            log.error("fetch_design failed", error=result["error"])
        else:
            files = result.get("files", [])
            local_folder = result.get("local_folder", "")
            log.info("fetch_design ok", files_count=len(files), local_folder=local_folder)
            print(f"[{_AGENT_ID}] fetch_design returned: folder={local_folder!r} files={files}")
        return ToolResult(output=json.dumps(result))


class FetchFigmaPage(BaseTool):
    name = "fetch_figma_page"
    description = "Fetch Figma page metadata and persist design data to the workspace."
    parameters_schema = {
        "type": "object",
        "properties": {
            "figma_url": {"type": "string"},
            "page_name": {"type": "string"},
            "workspace_path": {"type": "string"},
            "task_id": {"type": "string"},
        },
        "required": ["figma_url", "workspace_path"],
    }

    def execute_sync(
        self,
        figma_url: str = "",
        page_name: str = "",
        workspace_path: str = "",
        task_id: str = "",
    ) -> ToolResult:
        result = json.loads(FetchDesign().execute_sync(
            figma_url=figma_url,
            task_id=task_id,
            workspace_path=workspace_path,
        ).output)
        pages = result.get("pages", [])
        if page_name and pages:
            wanted = page_name.lower()
            result["matched_page"] = next(
                (page for page in pages if wanted in str(page.get("name", "")).lower()),
                None,
            )
        return ToolResult(output=json.dumps(result))


class FetchStitchScreen(BaseTool):
    name = "fetch_stitch_screen"
    description = "Fetch a Google Stitch screen and persist code, spec, and screenshot files."
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "screen_id": {"type": "string"},
            "screen_name": {"type": "string"},
            "workspace_path": {"type": "string"},
            "task_id": {"type": "string"},
        },
        "required": ["project_id", "workspace_path"],
    }

    def execute_sync(
        self,
        project_id: str = "",
        screen_id: str = "",
        screen_name: str = "",
        workspace_path: str = "",
        task_id: str = "",
    ) -> ToolResult:
        return FetchDesign().execute_sync(
            stitch_project_id=project_id,
            stitch_screen_id=screen_id,
            screen_name=screen_name,
            task_id=task_id,
            workspace_path=workspace_path,
        )


class FetchDesignTokens(BaseTool):
    name = "fetch_design_tokens"
    description = "Fetch available design tokens from Figma styles or Stitch project metadata."
    parameters_schema = {
        "type": "object",
        "properties": {
            "figma_url": {"type": "string"},
            "stitch_project_id": {"type": "string"},
            "task_id": {"type": "string"},
        },
        "required": [],
    }

    def execute_sync(
        self,
        figma_url: str = "",
        stitch_project_id: str = "",
        task_id: str = "",
    ) -> ToolResult:
        log = _log(task_id)
        adapter = _get_adapter()
        if figma_url:
            result = adapter._dispatch("figma.styles.fetch", figma_url, {"metadata": {"figmaUrl": figma_url}})
            return ToolResult(output=json.dumps({
                "tokens": result.get("styles", result),
                "status": result.get("status", "ok"),
            }))
        if stitch_project_id:
            result = adapter._dispatch(
                "stitch.project.get",
                stitch_project_id,
                {"metadata": {"stitchProjectId": stitch_project_id}},
            )
            project = result.get("project", result)
            tokens = {}
            if isinstance(project, dict):
                tokens = project.get("designTheme") or project.get("tokens") or {}
            return ToolResult(output=json.dumps({"tokens": tokens, "status": result.get("status", "ok")}))
        log.warn("fetch_design_tokens: no design source provided")
        return ToolResult(output=json.dumps({"tokens": {}, "status": "no_source"}))


class ExportDesignScreenshot(BaseTool):
    name = "export_design_screenshot"
    description = "Export or persist a design screenshot PNG in the workspace."
    parameters_schema = {
        "type": "object",
        "properties": {
            "figma_url": {"type": "string"},
            "stitch_project_id": {"type": "string"},
            "stitch_screen_id": {"type": "string"},
            "workspace_path": {"type": "string"},
            "task_id": {"type": "string"},
        },
        "required": ["workspace_path"],
    }

    def execute_sync(
        self,
        figma_url: str = "",
        stitch_project_id: str = "",
        stitch_screen_id: str = "",
        workspace_path: str = "",
        task_id: str = "",
    ) -> ToolResult:
        if stitch_project_id:
            result = json.loads(FetchDesign().execute_sync(
                stitch_project_id=stitch_project_id,
                stitch_screen_id=stitch_screen_id,
                task_id=task_id,
                workspace_path=workspace_path,
            ).output)
            image_path = result.get("design_screen_path", "")
            return ToolResult(output=json.dumps({
                "status": "ok" if image_path else "missing_image",
                "image_path": image_path,
                **result,
            }))
        if figma_url:
            result = json.loads(FetchDesign().execute_sync(
                figma_url=figma_url,
                task_id=task_id,
                workspace_path=workspace_path,
            ).output)
            return ToolResult(output=json.dumps({"status": "not_supported", "image_path": "", **result}))
        return ToolResult(output=json.dumps({"status": "no_source", "image_path": ""}))


def _save_figma_files(result: dict, workspace_path: str, log) -> dict:
    """Save Figma design data to <workspace>/ui-design/figma/."""
    import json as _json
    import time as _time
    try:
        local_folder = os.path.join(workspace_path, _AGENT_ID, "figma")
        os.makedirs(local_folder, exist_ok=True)
        spec_file = os.path.join(local_folder, "design-spec.json")
        with open(spec_file, "w", encoding="utf-8") as fh:
            _json.dump({
                "metadata": {"agent_id": _AGENT_ID, "step": "fetch_design",
                             "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z")},
                "data": result,
            }, fh, ensure_ascii=False, indent=2)
        log.info("figma design saved", local_folder=local_folder)
        print(f"[{_AGENT_ID}] Figma design saved to {local_folder}")
        return {"local_folder": local_folder, "files": [f"ui-design/figma/design-spec.json"]}
    except OSError as exc:
        log.warn("figma save failed", error=str(exc))
        return {}


def _save_stitch_files(
    screen_result: dict,
    project_result: dict,
    project_id: str,
    screen_id: str,
    workspace_path: str,
    log,
) -> dict:
    """Download and save Stitch design files to <workspace>/ui-design/stitch/.

    Saves:
      - DESIGN.md  — design spec built from project metadata + screen metadata
      - code.html  — HTML implementation downloaded from htmlCode.downloadUrl
      - screen.png — screenshot downloaded from screenshot.downloadUrl
      - screen-meta.json — raw screen metadata for traceability
    """
    import json as _json
    import time as _time
    from urllib.request import Request as _Req, urlopen as _urlopen

    local_folder = os.path.join(workspace_path, _AGENT_ID, "stitch")
    try:
        os.makedirs(local_folder, exist_ok=True)
    except OSError as exc:
        log.warn("stitch folder create failed", error=str(exc))
        return {}

    files = []

    # --- Parse screen metadata (may be JSON in the text field) ---
    screen_data = screen_result.get("screen", {})
    screen_text = screen_data.get("text", "")
    screen_meta = {}
    if screen_text and screen_text.strip().startswith("{"):
        try:
            screen_meta = _json.loads(screen_text.strip())
        except Exception:
            pass

    html_content = ""
    screenshot_bytes = b""

    # --- Download HTML code ---
    html_download_url = (screen_meta.get("htmlCode") or {}).get("downloadUrl", "")
    if html_download_url:
        try:
            req = _Req(html_download_url, headers={"User-Agent": "constellation-ui-design/1.0"})
            with _urlopen(req, timeout=30) as resp:
                html_content = resp.read().decode("utf-8", errors="replace")
            log.info("stitch HTML downloaded", chars=len(html_content), url=html_download_url[:80])
        except Exception as exc:
            log.warn("stitch HTML download failed", error=str(exc))

    if html_content:
        html_file = os.path.join(local_folder, "code.html")
        with open(html_file, "w", encoding="utf-8") as fh:
            fh.write(html_content)
        files.append(f"ui-design/stitch/code.html")
        log.info("stitch code.html saved", chars=len(html_content), path=html_file)
        print(f"[{_AGENT_ID}] Saved code.html ({len(html_content)} chars) to {html_file}")

    # --- Download screenshot ---
    screenshot_url = (screen_meta.get("screenshot") or {}).get("downloadUrl", "")
    if screenshot_url:
        try:
            req = _Req(screenshot_url, headers={"User-Agent": "constellation-ui-design/1.0"})
            with _urlopen(req, timeout=30) as resp:
                screenshot_bytes = resp.read()
            log.info("stitch screenshot downloaded", bytes=len(screenshot_bytes), url=screenshot_url[:80])
        except Exception as exc:
            log.warn("stitch screenshot download failed", error=str(exc))

    if screenshot_bytes:
        screen_png = os.path.join(local_folder, "screen.png")
        with open(screen_png, "wb") as fh:
            fh.write(screenshot_bytes)
        files.append(f"ui-design/stitch/screen.png")
        log.info("stitch screen.png saved", bytes=len(screenshot_bytes), path=screen_png)
        print(f"[{_AGENT_ID}] Saved screen.png ({len(screenshot_bytes)} bytes) to {screen_png}")

    # --- Build and save DESIGN.md from project + screen metadata ---
    design_md = _build_design_md(project_result, screen_meta, project_id, screen_id)
    if design_md:
        design_file = os.path.join(local_folder, "DESIGN.md")
        with open(design_file, "w", encoding="utf-8") as fh:
            fh.write(design_md)
        files.append(f"ui-design/stitch/DESIGN.md")
        log.info("stitch DESIGN.md saved", chars=len(design_md), path=design_file)
        print(f"[{_AGENT_ID}] Saved DESIGN.md ({len(design_md)} chars) to {design_file}")

    # --- Save raw screen metadata for traceability ---
    meta_file = os.path.join(local_folder, "screen-meta.json")
    try:
        with open(meta_file, "w", encoding="utf-8") as fh:
            _json.dump({
                "metadata": {"agent_id": _AGENT_ID, "step": "fetch_design",
                             "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                             "project_id": project_id, "screen_id": screen_id},
                "screen": screen_result,
                "project": project_result,
                "screen_meta": screen_meta,
            }, fh, ensure_ascii=False, indent=2)
        files.append(f"ui-design/stitch/screen-meta.json")
    except OSError as exc:
        log.warn("stitch meta save failed", error=str(exc))

    print(f"[{_AGENT_ID}] Stitch files saved to {local_folder}: {files}")
    return {
        "local_folder": local_folder,
        "files": files,
        "design_code_path": os.path.join(local_folder, "code.html") if html_content else "",
        "design_screen_path": os.path.join(local_folder, "screen.png") if screenshot_bytes else "",
        "design_md_path": os.path.join(local_folder, "DESIGN.md") if design_md else "",
    }


def _build_design_md(project_result: dict, screen_meta: dict, project_id: str, screen_id: str) -> str:
    """Build a DESIGN.md from Stitch project metadata and screen metadata.

    Priority:
    1. project.designTheme.designMd  — pre-rendered Markdown/YAML from Stitch API
    2. project.text / project.raw    — free-form text content
    3. Fallback: basic spec from screen metadata only
    """
    project_data = project_result.get("project", project_result)

    # --- Priority 1: designTheme.designMd (Stitch API v2+) ---
    if isinstance(project_data, dict):
        design_md = (
            project_data.get("designTheme", {}) or {}
        ).get("designMd", "")
        if design_md and ("---" in design_md or "colors:" in design_md or "typography:" in design_md):
            return design_md.strip()

    # --- Priority 2: text content blocks ---
    project_text = ""
    if isinstance(project_data, dict):
        content = project_data.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text", "")
                    if t:
                        project_text += t + "\n"
        if not project_text:
            project_text = project_data.get("text", "") or project_data.get("raw", "")

    # If project text has YAML frontmatter (design tokens), use it as DESIGN.md
    if project_text and ("---" in project_text or "colors:" in project_text or "typography:" in project_text):
        return project_text.strip()

    # If project text is JSON metadata, try to extract design info
    if project_text and project_text.strip().startswith("{"):
        try:
            import json as _json
            proj_meta = _json.loads(project_text.strip())
            project_text = ""  # reset to rebuild from structured data
            # Extract available fields
            name = proj_meta.get("displayName", "") or proj_meta.get("name", "") or ""
            project_text = f"# {name}\n\nProject ID: {project_id}\n\n"
        except Exception:
            pass

    # Build basic DESIGN.md from screen metadata
    title = screen_meta.get("title", "Design Screen")
    width = screen_meta.get("width", "")
    height = screen_meta.get("height", "")
    device = screen_meta.get("deviceType", "")

    lines = [
        f"# {title}",
        "",
        f"**Screen:** {title}",
        f"**Size:** {width}x{height} px ({device})" if width else "",
        f"**Project ID:** {project_id}",
        f"**Screen ID:** {screen_id}",
        "",
    ]
    if project_text:
        lines.append(project_text)

    lines += [
        "## Design Reference",
        "",
        "This screen design spec was fetched from Google Stitch.",
        "Refer to `code.html` for the generated HTML/CSS implementation reference.",
        "Refer to `screen.png` for the visual design screenshot.",
        "",
        "## Implementation Notes",
        "",
        "- Match the visual design pixel-by-pixel from the reference screenshot.",
        "- Use the HTML/CSS in `code.html` as the component reference implementation.",
        "- Preserve all color values, typography, spacing, and layout from the design.",
        "- Check every component: header, navigation, cards, buttons, footer, etc.",
    ]

    return "\n".join(l for l in lines if l is not None)


_TOOLS = [
    FetchDesign(),
    FetchFigmaPage(),
    FetchStitchScreen(),
    FetchDesignTokens(),
    ExportDesignScreenshot(),
]


def register_ui_design_tools() -> None:
    """Register in-process UI design tools (idempotent, won't override existing)."""
    registry = get_registry()
    existing = {s["function"]["name"] for s in registry.list_schemas()}
    for tool in _TOOLS:
        if tool.name not in existing:
            registry.register(tool)
    print(f"[ui-design-tools] Registered: {[t.name for t in _TOOLS if t.name not in existing]}")
