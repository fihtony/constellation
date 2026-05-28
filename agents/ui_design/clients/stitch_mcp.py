"""Google Stitch MCP client for the UI Design boundary adapter.

Communicates with the Stitch MCP server at https://stitch.googleapis.com/mcp
using JSON-RPC 2.0 over HTTPS POST.  Stdlib-only.

Authentication: X-Goog-Api-Key header (Google / Gemini API key).
Set STITCH_API_KEY in the environment or pass ``api_key`` to the constructor.

Stitch MCP workflow skill:
  .github/skills/stitch-mcp-workflow/SKILL.md
"""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

_STITCH_MCP_URL = "https://stitch.googleapis.com/mcp"


class StitchMcpClient:
    """Client for the Google Stitch MCP server.

    Parameters
    ----------
    api_key:
        Google / Gemini API key.  Falls back to ``STITCH_API_KEY`` env var.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = (api_key or os.environ.get("STITCH_API_KEY", "")).strip()
        self._call_id = 0

    # ------------------------------------------------------------------
    # High-level helpers
    # ------------------------------------------------------------------

    def list_tools(self, timeout: int = 30) -> tuple[list, str]:
        """Discover available Stitch MCP tools."""
        resp = self._post("tools/list", {}, timeout=timeout)
        if "error" in resp:
            return [], _error_status(resp)
        tools = resp.get("result", {}).get("tools", [])
        return tools, "ok"

    def get_project(self, project_id: str, timeout: int = 60) -> tuple[dict, str]:
        """Fetch Stitch project metadata."""
        return self._call_tool(
            "get_project",
            {"name": f"projects/{project_id}"},
            timeout,
        )

    def list_screens(self, project_id: str, timeout: int = 60) -> tuple[list, str]:
        """List all screens in a project.

        Returns a list of ``{id, name}`` dicts.
        """
        data, status = self._call_tool(
            "list_screens",
            {"project_id": project_id},
            timeout,
        )
        if status != "ok":
            return [], status
        if isinstance(data, list):
            return data, "ok"
        # Unwrap various response shapes
        screens = data.get("screens") or data.get("items") or []
        if not screens:
            raw = data.get("raw", "")
            if raw:
                screens = _parse_screens_text(raw)
        return screens, "ok"

    def find_screen_by_name(
        self,
        project_id: str,
        name: str,
        timeout: int = 60,
    ) -> tuple[dict | None, str]:
        """Find a screen by name (case-insensitive, partial match).

        Match priority:
        1. Exact case-insensitive
        2. Search term is a substring of screen name
        3. Screen name is a substring of search term
        """
        screens, status = self.list_screens(project_id, timeout)
        if not screens:
            return None, status
        name_lower = name.lower()
        for s in screens:
            if s.get("name", "").lower() == name_lower:
                return s, "ok"
        for s in screens:
            if name_lower in s.get("name", "").lower():
                return s, "ok"
        for s in screens:
            if s.get("name", "").lower() in name_lower:
                return s, "ok"
        return None, "not_found"

    def get_screen(
        self,
        project_id: str,
        screen_id: str,
        timeout: int = 60,
    ) -> tuple[dict, str]:
        """Fetch screen design specification and generated code.

        Returns a dict with keys:
          - ``text``: concatenated text of ALL content blocks (HTML code + design
            markdown/YAML joined by newline). Team Lead writes this to workspace.
          - ``content``: raw content array from the MCP response.
          - ``projectId``, ``screenId``: echo of inputs for traceability.
          - ``imageUrls``: list of image URLs found in content blocks.
        """
        resp = self._post(
            "tools/call",
            {"name": "get_screen", "arguments": {"project_id": project_id, "screen_id": screen_id}},
            timeout=timeout,
        )
        if "error" in resp:
            return {}, _error_status(resp)
        result = resp.get("result", {})
        if result.get("isError"):
            fallback = self._screen_from_list_screens(project_id, screen_id, timeout)
            if fallback:
                return fallback, "ok"
            return {}, "tool_error"
        content = result.get("content", [])
        if isinstance(content, list):
            # Collect ALL text/resource blocks:
            # - text blocks: HTML code and/or design YAML/markdown
            # - resource blocks: may contain embedded HTML or image data
            # - image blocks: direct image URLs
            text_parts = []
            image_urls = []
            embedded_html = ""
            for block in content:
                btype = block.get("type", "")
                if btype == "text" and block.get("text"):
                    text_parts.append(block["text"])
                elif btype == "resource":
                    # Resource blocks may have embedded data (HTML) or a URL
                    resource = block.get("resource", {})
                    blob = block.get("blob", "") or resource.get("blob", "")
                    res_text = block.get("text", "") or resource.get("text", "")
                    uri = block.get("uri", "") or resource.get("uri", "")
                    mime = block.get("mimeType", "") or resource.get("mimeType", "")
                    if res_text and "html" in mime.lower():
                        embedded_html = res_text
                    elif res_text:
                        text_parts.append(res_text)
                    if blob and "html" in mime.lower():
                        embedded_html = blob
                    if uri:
                        image_urls.append(uri)
                elif btype == "image":
                    url = block.get("url", "") or block.get("source", {}).get("url", "")
                    if url:
                        image_urls.append(url)
            text = "\n".join(text_parts)
            # If HTML was embedded in a resource block, prepend it to text
            if embedded_html and "<!DOCTYPE" in embedded_html or (embedded_html and "<html" in embedded_html):
                text = embedded_html + "\n" + text if text else embedded_html
            return {
                "projectId": project_id,
                "screenId": screen_id,
                "content": content,
                "text": text,
                "imageUrls": image_urls,
            }, "ok"
        return result, "ok"

    def _screen_from_list_screens(
        self,
        project_id: str,
        screen_id: str,
        timeout: int = 60,
    ) -> dict | None:
        """Fallback for transient get_screen failures.

        Stitch's list_screens response already includes the screen HTML and
        screenshot download URLs, which is enough for workspace persistence.
        """
        screens, status = self.list_screens(project_id, timeout)
        if status != "ok":
            return None

        matched = _match_screen_entry(screens, screen_id)
        if not matched:
            return None

        screenshot_url = (matched.get("screenshot") or {}).get("downloadUrl", "")
        image_urls = [screenshot_url] if screenshot_url else []
        text = json.dumps(matched, ensure_ascii=False)
        return {
            **matched,
            "projectId": project_id,
            "screenId": _screen_entry_id(matched) or screen_id,
            "content": [{"type": "text", "text": text}],
            "text": text,
            "imageUrls": image_urls,
        }

    def get_screen_image(
        self,
        project_id: str,
        screen_id: str,
        timeout: int = 60,
    ) -> tuple[dict, str]:
        """Fetch screen image URL.

        Returns ``({}, "tool_not_found")`` when the tool is unavailable in
        the current Stitch MCP version — treat gracefully.
        """
        data, status = self._call_tool(
            "get_screen_image",
            {"project_id": project_id, "screen_id": screen_id},
            timeout,
        )
        if status == "tool_error":
            return {}, "tool_not_found"
        return data, status

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._call_id += 1
        return self._call_id

    def _post(self, method: str, params: dict, timeout: int = 60) -> dict:
        """Send a JSON-RPC 2.0 request to the Stitch MCP server."""
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params,
        }).encode("utf-8")
        req = Request(
            _STITCH_MCP_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": self._api_key,
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return {"error": {"code": exc.code, "message": str(exc)}}
        except Exception as exc:
            return {"error": {"code": -1, "message": str(exc)}}

    def _call_tool(
        self, tool_name: str, arguments: dict, timeout: int = 60
    ) -> tuple[dict, str]:
        """Call a Stitch MCP tool and unwrap the result."""
        resp = self._post(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout=timeout,
        )
        if "error" in resp:
            return {}, _error_status(resp)
        result = resp.get("result", {})
        if result.get("isError"):
            return {}, "tool_error"
        content = result.get("content", [])
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    try:
                        return json.loads(block["text"]), "ok"
                    except (json.JSONDecodeError, KeyError):
                        return {"raw": block.get("text", "")}, "ok"
        return result, "ok"


# ------------------------------------------------------------------
# Module helpers
# ------------------------------------------------------------------

def _error_status(resp: dict) -> str:
    code = resp.get("error", {}).get("code", "unknown")
    return f"error_{code}"


def _screen_entry_id(screen: dict) -> str:
    name = str(screen.get("name", ""))
    if "/screens/" in name:
        return name.rsplit("/", 1)[-1]
    return str(screen.get("id", ""))


def _match_screen_entry(screens: list[dict], screen_id: str) -> dict | None:
    if not screen_id:
        return None

    normalized = screen_id.strip()
    for screen in screens:
        entry_id = _screen_entry_id(screen)
        name = str(screen.get("name", ""))
        if entry_id == normalized or name == normalized or name.endswith(f"/{normalized}"):
            return screen
    return None


def _parse_screens_text(text: str) -> list[dict]:
    """Parse plain-text screen listing in ``name: id`` format."""
    screens = []
    for line in text.strip().splitlines():
        parts = line.split(":", 1)
        if len(parts) == 2:
            screens.append({"name": parts[0].strip(), "id": parts[1].strip()})
    return screens
