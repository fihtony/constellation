"""UI Design Agent — Figma (REST API) + Google Stitch (MCP) design data access."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import threading
from urllib.parse import parse_qs, urlparse

from common.devlog import debug_log, record_workspace_stage
from common.env_utils import load_dotenv
from common.instance_reporter import InstanceReporter
from common.message_utils import build_text_artifact, extract_text
from common.rules_loader import build_system_prompt
from common.runtime.adapter import get_runtime, summarize_runtime_configuration

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Add this agent's own directory to sys.path so figma_client and stitch_client
# can be imported as local modules (they are specific to ui-design, not shared).
_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

import figma_client  # noqa: E402  (local to ui-design/)
import prompts as agent_prompts  # noqa: E402  (local to ui-design/)
import stitch_client  # noqa: E402  (local to ui-design/)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8040"))
AGENT_ID = os.environ.get("AGENT_ID", "ui-design-agent")
ADVERTISED_URL = os.environ.get("ADVERTISED_BASE_URL", f"http://ui-design:{PORT}")

_AGENT_CARD_PATH = os.path.join(os.path.dirname(__file__), "agent-card.json")
_TASK_SEQ = 0
_TASK_SEQ_LOCK = threading.Lock()
_TASKS: dict[str, dict] = {}


def _runtime_config_summary() -> dict:
    return {
        "runtime": summarize_runtime_configuration(),
        "provider": "figma+stitch",
    }


def _load_agent_card() -> dict:
    with open(_AGENT_CARD_PATH, encoding="utf-8") as fh:
        card = json.load(fh)
    text = json.dumps(card).replace("__ADVERTISED_URL__", ADVERTISED_URL)
    return json.loads(text)


def _next_task_id() -> str:
    global _TASK_SEQ
    with _TASK_SEQ_LOCK:
        _TASK_SEQ += 1
        return f"ui-design-task-{_TASK_SEQ:04d}"


# ---------------------------------------------------------------------------
# Request type detection
# ---------------------------------------------------------------------------

def _looks_like_figma_request(text: str) -> bool:
    lower = text.lower()
    return "figma" in lower or "www.figma.com" in lower


def _looks_like_stitch_request(text: str) -> bool:
    lower = text.lower()
    return (
        "stitch" in lower
        or "stitch.googleapis.com" in lower
        or "stitch.withgoogle.com" in lower
    )


# ---------------------------------------------------------------------------
# Workspace file helper
# ---------------------------------------------------------------------------

def _save_workspace_file(workspace_path: str, relative_name: str, content: str) -> None:
    """Write content to a file inside the shared workspace (best-effort)."""
    if not workspace_path:
        return
    try:
        full_path = os.path.join(workspace_path, relative_name)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        print(f"[{AGENT_ID}] Warning: could not save workspace file {relative_name}: {exc}")


def _run_agentic(
    prompt: str,
    actor: str,
    *,
    system_prompt: str | None = None,
    context: dict | None = None,
    timeout: int = 120,
    max_tokens: int = 4096,
) -> str:
    result = get_runtime().run(
        prompt=prompt,
        context=context,
        system_prompt=system_prompt,
        timeout=timeout,
        max_tokens=max_tokens,
    )
    for warning in result.get("warnings") or []:
        print(f"[{AGENT_ID}] Runtime warning ({actor}): {warning}")
    return result.get("raw_response") or result.get("summary") or ""


# ---------------------------------------------------------------------------
# Figma handler
# ---------------------------------------------------------------------------

def _handle_figma_message(user_text: str, capability: str) -> tuple[str, list]:
    import re

    url_match = re.search(r"https?://www\.figma\.com/[^\s\"']+", user_text)
    figma_url = url_match.group(0) if url_match else ""

    page_match = re.search(
        r'page(?:\s+name)?[:\s]+["\']?(.+?)["\']?(?:\s+https?://|$)',
        user_text, re.IGNORECASE,
    )
    page_name = page_match.group(1).strip(" :") if page_match else ""

    summary_parts: list[str] = []

    if figma_url:
        file_key, node_id = figma_client.parse_figma_url(figma_url)
        if file_key:
            meta, meta_status = figma_client.fetch_file_meta(file_key)
            if meta_status == "ok":
                summary_parts.append(
                    f"Figma file: {meta.get('name')} "
                    f"(last modified: {meta.get('lastModified')})"
                )
            else:
                summary_parts.append(f"Figma file metadata fetch status: {meta_status}")
            # Explicit element-spec request
            if capability == "figma.node.get" or node_id:
                node_result, node_status = figma_client.fetch_nodes(file_key, [node_id]) if node_id else ({}, "no_node_id")
                if node_status == "ok":
                    summary_parts.append(f"Element spec for node {node_id} fetched successfully.")
                else:
                    summary_parts.append(f"Node {node_id} fetch status: {node_status}")
            elif page_name:
                page_result, page_status = figma_client.fetch_page_by_name(
                    file_key, page_name
                )
                if page_status == "ok":
                    matched_page = page_result.get("page", {})
                    summary_parts.append(
                        f"Page matched: '{matched_page.get('name')}' "
                        f"(id: {matched_page.get('id')})"
                    )
                elif page_status == "page_not_found":
                    available = page_result.get("availablePages", [])
                    summary_parts.append(
                        f"Page '{page_name}' not found. Available: {available}"
                    )
                else:
                    summary_parts.append(
                        f"Figma page fetch status: {page_status}"
                    )
        else:
            summary_parts.append("Could not parse the Figma file key from the provided URL.")
    else:
        summary_parts.append("No Figma URL found in the request.")

    prompt = agent_prompts.FIGMA_SUMMARY_TEMPLATE.format(
        user_text=user_text,
        figma_url=figma_url or "none",
        page_name=page_name or "none",
        fetch_summary='; '.join(summary_parts) or 'no data fetched',
    )

    llm_text = _run_agentic(
        prompt,
        AGENT_ID,
        system_prompt=build_system_prompt(agent_prompts.FIGMA_SUMMARY_SYSTEM, "ui-design"),
    )
    if not str(llm_text).strip():
        llm_text = "; ".join(summary_parts) or "No Figma data fetched."
    artifacts = [
        build_text_artifact(
            "figma-summary",
            llm_text,
            artifact_type="application/vnd.ui-design.figma-summary",
            metadata={
                "agentId": AGENT_ID,
                "capability": capability or "figma.file.fetch",
                "figmaUrl": figma_url,
                "pageName": page_name,
            },
        )
    ]
    debug_log(AGENT_ID, "figma.message.completed", figmaUrl=figma_url, pageName=page_name)
    return "; ".join(summary_parts) or "Figma request processed.", artifacts


# ---------------------------------------------------------------------------
# Stitch handler
# ---------------------------------------------------------------------------

def _handle_stitch_message(user_text: str, capability: str, workspace_path: str = "") -> tuple[str, list]:
    import re

    proj_match = re.search(r"\b(\d{15,20})\b", user_text)
    project_id = proj_match.group(1) if proj_match else ""

    screen_match = re.search(r"\b([0-9a-f]{32})\b", user_text, re.IGNORECASE)
    screen_id = screen_match.group(1) if screen_match else ""

    # Extract page/screen name from text:
    #   "page: Landing Page (Bare-bones)"  or  "screen name: Foo Bar"  or  "page name 'Foo'"
    page_name = ""
    page_name_patterns = [
        r'(?:page|screen)[_\s-]*name[:\s]+["\']?([^"\'\\n,]+?)["\']?(?:\s|$)',
        r'(?:page|screen)[:\s]+["\']([^"\']+)["\']',
        r'(?:page|screen)[:\s]+([A-Z][^\n,]{3,60}?)(?:\s*$|\s*,)',
    ]
    for pat in page_name_patterns:
        m = re.search(pat, user_text, re.IGNORECASE)
        if m:
            page_name = m.group(1).strip().rstrip(".")
            break

    summary_parts: list[str] = []
    design_result: dict = {}

    if project_id and screen_id:
        result, status = stitch_client.get_screen(project_id, screen_id)
        if status == "ok":
            summary_parts.append(
                f"Stitch screen fetched: project={project_id}, screen={screen_id}"
            )
            if result.get("imageUrls"):
                summary_parts.append(f"Image URLs: {result['imageUrls'][:2]}")
            design_result = result
        else:
            summary_parts.append(f"Stitch screen fetch failed: {status}")
    elif project_id and page_name:
        # Try to resolve page name → screen ID using list_screens
        found_screen, find_status = stitch_client.find_screen_by_name(project_id, page_name)
        if found_screen:
            resolved_id = found_screen.get("id", "") or found_screen.get("screenId", "")
            resolved_name = found_screen.get("name", page_name)
            summary_parts.append(
                f"Resolved page '{page_name}' → screen '{resolved_name}' (id={resolved_id})"
            )
            if resolved_id:
                result, status = stitch_client.get_screen(project_id, resolved_id)
                if status == "ok":
                    summary_parts.append(
                        f"Stitch screen fetched: project={project_id}, screen={resolved_id}"
                    )
                    if result.get("imageUrls"):
                        summary_parts.append(f"Image URLs: {result['imageUrls'][:2]}")
                    design_result = result
                    screen_id = resolved_id
                else:
                    summary_parts.append(f"Stitch screen fetch failed after name resolution: {status}")
            else:
                summary_parts.append(f"Found screen by name but no ID available: {found_screen}")
        else:
            summary_parts.append(f"Page '{page_name}' not found in project {project_id}: {find_status}")
            # Still fetch project metadata as fallback
            result, status = stitch_client.get_project(project_id)
            if status == "ok":
                summary_parts.append(f"Stitch project fetched (fallback): {project_id}")
                design_result = result
    elif project_id:
        result, status = stitch_client.get_project(project_id)
        if status == "ok":
            summary_parts.append(f"Stitch project fetched: {project_id}")
            design_result = result
        else:
            summary_parts.append(f"Stitch project fetch failed: {status}")

    # Enrich design_result with local reference files if STITCH_LOCAL_REFS is configured.
    # Format: "screenId1:/path/to/ref1,screenId2:/path/to/ref2"
    _local_ref_map: dict[str, str] = {}
    for _entry in os.environ.get("STITCH_LOCAL_REFS", "").split(","):
        _entry = _entry.strip()
        if ":" in _entry:
            _sid, _rpath = _entry.split(":", 1)
            _local_ref_map[_sid.strip().lower()] = _rpath.strip()
    _local_ref_path = (
        _local_ref_map.get((screen_id or "").lower())
        or _local_ref_map.get((project_id or "").lower())
        or ""
    )
    if _local_ref_path and os.path.isdir(_local_ref_path):
        import shutil as _shutil
        _screen_png = os.path.join(_local_ref_path, "screen.png")
        if workspace_path and os.path.isfile(_screen_png):
            _dest = os.path.join(workspace_path, "ui-design", "design-reference.png")
            os.makedirs(os.path.dirname(_dest), exist_ok=True)
            _shutil.copy2(_screen_png, _dest)
            summary_parts.append("Local design reference screenshot saved to workspace")
        _code_html_path = os.path.join(_local_ref_path, "code.html")
        _design_md_path = os.path.join(_local_ref_path, "DESIGN.md")
        if os.path.isfile(_code_html_path):
            with open(_code_html_path, encoding="utf-8") as _fh:
                design_result["localCodeHtml"] = _fh.read()
            summary_parts.append(f"Local code.html loaded ({len(design_result['localCodeHtml'])} chars)")
        if os.path.isfile(_design_md_path):
            with open(_design_md_path, encoding="utf-8") as _fh:
                design_result["localDesignMd"] = _fh.read()
            summary_parts.append("Local DESIGN.md loaded")

    # Save design content to shared workspace
    if workspace_path and design_result:
        _save_workspace_file(
            workspace_path,
            "ui-design/stitch-design.json",
            json.dumps(design_result, ensure_ascii=False, indent=2),
        )
        debug_log(AGENT_ID, "stitch.workspace.saved", path=workspace_path)

    prompt = agent_prompts.STITCH_SUMMARY_TEMPLATE.format(
        user_text=user_text,
        project_id=project_id or "none",
        screen_id=screen_id or "none",
        page_name=page_name or "none",
        fetch_summary='; '.join(summary_parts) or 'no data fetched',
    )

    llm_text = _run_agentic(
        prompt,
        AGENT_ID,
        system_prompt=build_system_prompt(agent_prompts.STITCH_SUMMARY_SYSTEM, "ui-design"),
    )
    artifacts = [
        build_text_artifact(
            "stitch-summary",
            llm_text,
            artifact_type="application/vnd.ui-design.stitch-summary",
            metadata={
                "agentId": AGENT_ID,
                "capability": capability or "stitch.screen.fetch",
                "projectId": project_id,
                "screenId": screen_id,
                "pageName": page_name,
            },
        )
    ]
    debug_log(AGENT_ID, "stitch.message.completed", projectId=project_id, screenId=screen_id, pageName=page_name)
    return "; ".join(summary_parts) or "Stitch request processed.", artifacts


# ---------------------------------------------------------------------------
# Generic handler
# ---------------------------------------------------------------------------

def _handle_generic_message(user_text: str) -> tuple[str, list]:
    prompt = agent_prompts.GENERIC_TEMPLATE.format(user_text=user_text)
    llm_text = _run_agentic(
        prompt,
        AGENT_ID,
        system_prompt=build_system_prompt(agent_prompts.GENERIC_SYSTEM, "ui-design"),
    )
    artifacts = [
        build_text_artifact(
            "ui-design-response",
            llm_text,
            artifact_type="application/vnd.ui-design.response",
            metadata={"agentId": AGENT_ID, "capability": "ui-design.general"},
        )
    ]
    return "Request processed.", artifacts


# ---------------------------------------------------------------------------
# Message dispatcher
# ---------------------------------------------------------------------------

def _dispatch_message(message: dict) -> tuple[str, list]:
    user_text = extract_text(message)
    metadata = message.get("metadata", {}) if isinstance(message, dict) else {}
    capability = metadata.get("requestedCapability", "")
    workspace_path = metadata.get("sharedWorkspacePath", "")

    debug_log(AGENT_ID, "ui-design.message.received",
              userText=user_text, capability=capability)
    if workspace_path:
        record_workspace_stage(
            workspace_path,
            "ui-design",
            f"Started {capability or 'ui-design request'}",
            task_id=(metadata.get("orchestratorTaskId") or ""),
                extra={"runtimeConfig": _runtime_config_summary()},
        )

    if capability.startswith("figma.") or _looks_like_figma_request(user_text):
        return _handle_figma_message(user_text, capability)

    if capability.startswith("stitch.") or _looks_like_stitch_request(user_text):
        return _handle_stitch_message(user_text, capability, workspace_path=workspace_path)

    return _handle_generic_message(user_text)


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------

def _fire_callback(
    callback_url: str, task_id: str, state: str,
    status_message: str, artifacts: list,
) -> None:
    from urllib.request import Request as Req, urlopen as _urlopen

    payload = {
        "downstreamTaskId": task_id,
        "state": state,
        "statusMessage": status_message,
        "artifacts": artifacts,
        "agentId": AGENT_ID,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Req(
        callback_url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with _urlopen(req, timeout=10):
            pass
    except Exception as exc:
        print(f"[{AGENT_ID}] Callback failed: {exc}", flush=True)


def _run_task_background(
    task_id: str, message: dict,
) -> None:
    metadata = message.get("metadata", {}) if isinstance(message, dict) else {}
    workspace_path = metadata.get("sharedWorkspacePath", "")
    capability = metadata.get("requestedCapability", "")
    try:
        status_text, artifacts = _dispatch_message(message)
        if workspace_path:
            record_workspace_stage(
                workspace_path,
                "ui-design",
                f"Completed {capability or 'ui-design request'}",
                task_id=task_id,
                    extra={"statusText": status_text, "runtimeConfig": _runtime_config_summary()},
            )
        _TASKS[task_id] = {
            "id": task_id,
            "agentId": AGENT_ID,
            "status": {
                "state": "TASK_STATE_COMPLETED",
                "message": {
                    "role": "ROLE_AGENT",
                    "parts": [{"text": status_text}],
                },
            },
            "artifacts": artifacts,
        }
        callback_url = metadata.get("orchestratorCallbackUrl", "")
        if callback_url:
            _fire_callback(
                callback_url, task_id, "TASK_STATE_COMPLETED", status_text, artifacts
            )
    except Exception as exc:
        print(f"[{AGENT_ID}] Task {task_id} failed: {exc}", flush=True)
        if workspace_path:
            record_workspace_stage(
                workspace_path,
                "ui-design",
                f"Failed {capability or 'ui-design request'}",
                task_id=task_id,
                    extra={"error": str(exc), "runtimeConfig": _runtime_config_summary()},
            )
        _TASKS[task_id] = {
            "id": task_id,
            "agentId": AGENT_ID,
            "status": {
                "state": "TASK_STATE_FAILED",
                "message": {"role": "ROLE_AGENT", "parts": [{"text": str(exc)}]},
            },
            "artifacts": [],
        }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class UIDesignHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        line = args[0] if args else ""
        if any(p in line for p in ("/health", "/.well-known/agent-card.json")):
            return
        print(
            f"[{AGENT_ID}] {line} "
            f"{args[1] if len(args) > 1 else ''} "
            f"{args[2] if len(args) > 2 else ''}"
        )

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # -----------------------------------------------------------------------
    # GET routes
    # -----------------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/health":
            self._send_json(200, {"status": "ok", "service": AGENT_ID})
            return

        if path == "/.well-known/agent-card.json":
            self._send_json(200, _load_agent_card())
            return

        # --- Figma REST ---

        if path == "/figma/meta":
            figma_url = (qs.get("url") or [""])[0]
            if not figma_url:
                self._send_json(400, {"error": "missing url parameter"})
                return
            file_key, _ = figma_client.parse_figma_url(figma_url)
            if not file_key:
                self._send_json(400, {"error": "could_not_parse_figma_url"})
                return
            meta, status = figma_client.fetch_file_meta(file_key)
            self._send_json(
                200 if status == "ok" else 502,
                {"fileKey": file_key, "status": status, "meta": meta},
            )
            return

        if path == "/figma/pages":
            figma_url = (qs.get("url") or [""])[0]
            if not figma_url:
                self._send_json(400, {"error": "missing url parameter"})
                return
            file_key, _ = figma_client.parse_figma_url(figma_url)
            if not file_key:
                self._send_json(400, {"error": "could_not_parse_figma_url"})
                return
            pages, status = figma_client.fetch_pages(file_key)
            self._send_json(
                200 if status == "ok" else 502,
                {"fileKey": file_key, "status": status, "pages": pages},
            )
            return

        if path == "/figma/page":
            figma_url = (qs.get("url") or [""])[0]
            page_name = (qs.get("name") or [""])[0]
            if not figma_url or not page_name:
                self._send_json(400, {"error": "missing url or name parameter"})
                return
            file_key, _ = figma_client.parse_figma_url(figma_url)
            if not file_key:
                self._send_json(400, {"error": "could_not_parse_figma_url"})
                return
            result, status = figma_client.fetch_page_by_name(file_key, page_name)
            code = (
                200 if status == "ok"
                else 404 if status == "page_not_found"
                else 502
            )
            self._send_json(code, {"fileKey": file_key, "status": status, **result})
            return

        if path == "/figma/node":
            # Fetch design spec for a specific element/node.
            # Accept either: ?url=<figma_url> (node_id extracted from url)
            #            or: ?url=<figma_url>&node_id=<1:470>
            figma_url = (qs.get("url") or [""])[0]
            node_id = (qs.get("node_id") or [""])[0]
            if not figma_url:
                self._send_json(400, {"error": "missing url parameter"})
                return
            file_key, url_node_id = figma_client.parse_figma_url(figma_url)
            if not file_key:
                self._send_json(400, {"error": "could_not_parse_figma_url"})
                return
            node_id = node_id or url_node_id or ""
            if not node_id:
                self._send_json(400, {"error": "node_id required (pass as ?node_id= or embed in Figma URL)"})
                return
            result, status = figma_client.fetch_nodes(file_key, [node_id])
            self._send_json(
                200 if status == "ok" else 502,
                {"fileKey": file_key, "nodeId": node_id, "status": status, **result},
            )
            return

        # --- Stitch MCP ---

        if path == "/stitch/tools":
            tools, status = stitch_client.list_tools()
            self._send_json(
                200 if status == "ok" else 502,
                {"status": status, "tools": tools},
            )
            return

        if path == "/stitch/project":
            project_id = (qs.get("id") or [""])[0]
            if not project_id:
                self._send_json(400, {"error": "missing id parameter"})
                return
            result, status = stitch_client.get_project(project_id)
            self._send_json(
                200 if status == "ok" else 502,
                {"projectId": project_id, "status": status, **result},
            )
            return

        if path == "/stitch/screen":
            project_id = (qs.get("project_id") or [""])[0]
            screen_id = (qs.get("screen_id") or [""])[0]
            if not project_id or not screen_id:
                self._send_json(400, {"error": "missing project_id or screen_id"})
                return
            result, status = stitch_client.get_screen(project_id, screen_id)
            self._send_json(
                200 if status == "ok" else 502,
                {
                    "projectId": project_id,
                    "screenId": screen_id,
                    "status": status,
                    **result,
                },
            )
            return

        if path == "/stitch/screen/image":
            project_id = (qs.get("project_id") or [""])[0]
            screen_id = (qs.get("screen_id") or [""])[0]
            if not project_id or not screen_id:
                self._send_json(400, {"error": "missing project_id or screen_id"})
                return
            result, status = stitch_client.get_screen_image(project_id, screen_id)
            self._send_json(
                200 if status in ("ok", "tool_not_found") else 502,
                {
                    "projectId": project_id,
                    "screenId": screen_id,
                    "status": status,
                    **result,
                },
            )
            return

        # --- Task polling ---

        if path.startswith("/tasks/"):
            task_id = path[len("/tasks/"):]
            if task_id in _TASKS:
                self._send_json(200, {"task": _TASKS[task_id]})
            else:
                self._send_json(404, {"error": "task_not_found"})
            return

        self._send_json(404, {"error": "not_found"})

    # -----------------------------------------------------------------------
    # POST routes
    # -----------------------------------------------------------------------

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/message:send":
            body = self._read_body()
            message = body.get("message", {})
            if not message:
                self._send_json(400, {"error": "missing message"})
                return

            task_id = _next_task_id()
            print(f"[{AGENT_ID}] Task {task_id} submitted", flush=True)

            _TASKS[task_id] = {
                "id": task_id,
                "agentId": AGENT_ID,
                "status": {"state": "TASK_STATE_WORKING"},
                "artifacts": [],
            }

            threading.Thread(
                target=_run_task_background,
                args=(task_id, message),
                daemon=True,
            ).start()

            self._send_json(200, {"task": _TASKS[task_id]})
            return

        self._send_json(404, {"error": "not_found"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    reporter = InstanceReporter(
        agent_id=AGENT_ID, service_url=ADVERTISED_URL, port=PORT
    )
    reporter.start()

    server = ThreadingHTTPServer((HOST, PORT), UIDesignHandler)
    print(f"[{AGENT_ID}] UI Design Agent starting on {HOST}:{PORT}", flush=True)
    print(f"[{AGENT_ID}] Advertised URL: {ADVERTISED_URL}", flush=True)
    try:
        server.serve_forever()
    finally:
        reporter.stop()


if __name__ == "__main__":
    main()
