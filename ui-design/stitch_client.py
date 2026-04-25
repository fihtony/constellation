"""Google Stitch MCP client — read-only design data fetching via JSON-RPC 2.0."""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

STITCH_MCP_URL = "https://stitch.googleapis.com/mcp"


def _stitch_api_key() -> str:
    return os.environ.get("STITCH_API_KEY", "").strip()


def _stitch_post(method: str, params: dict, timeout: int = 30) -> tuple[int, dict]:
    """POST a JSON-RPC 2.0 request to the Stitch MCP server.

    Returns (http_status, rpc_response_dict).
    """
    api_key = _stitch_api_key()
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if api_key:
        headers["X-Goog-Api-Key"] = api_key
    req = Request(STITCH_MCP_URL, data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body.strip() else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"error": body[:500]}
    except URLError as exc:
        return 0, {"error": str(exc.reason)}


def _extract_text_content(rpc_resp: dict) -> str:
    """Extract concatenated text content from an MCP tools/call result."""
    result = rpc_resp.get("result", {})
    if isinstance(result, dict):
        content = result.get("content", [])
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(parts)
    return str(result)


def list_tools() -> tuple[list, str]:
    """List all available Stitch MCP tools.

    Returns (tools_list, status) where status is "ok" or an error string.
    """
    status, body = _stitch_post("tools/list", {})
    if status == 200 and "result" in body:
        tools = (body["result"] or {}).get("tools", [])
        return tools, "ok"
    if status == 200 and "error" in body:
        return [], f"error: {body['error'].get('message', str(body['error']))}"
    return [], f"error_{status}"


def get_project(project_id: str) -> tuple[dict, str]:
    """Fetch project metadata for the given Stitch project ID.

    Returns (result_dict, status).
    """
    status, body = _stitch_post(
        "tools/call",
        {"name": "get_project", "arguments": {"name": f"projects/{project_id}"}},
    )
    if status == 200 and "result" in body:
        result = body["result"]
        if isinstance(result, dict) and result.get("isError"):
            text = _extract_text_content(body)
            return {"error": text}, f"stitch_error"
        content = result.get("content", [])
        text = _extract_text_content(body)
        return {"projectId": project_id, "content": content, "text": text}, "ok"
    if status == 200 and "error" in body:
        return body, f"rpc_error"
    return body, f"error_{status}"


def get_screen(project_id: str, screen_id: str) -> tuple[dict, str]:
    """Fetch screen design and code for the given screen in a Stitch project.

    Returns (result_dict, status).
    """
    status, body = _stitch_post(
        "tools/call",
        {
            "name": "get_screen",
            "arguments": {"project_id": project_id, "screen_id": screen_id},
        },
    )
    if status == 200 and "result" in body:
        result = body["result"]
        if isinstance(result, dict) and result.get("isError"):
            text = _extract_text_content(body)
            return {"error": text}, "stitch_error"
        content = result.get("content", [])
        text = _extract_text_content(body)
        # Extract image URLs from content items
        image_urls = [
            item.get("url", "") or item.get("data", "")[:80]
            for item in content
            if isinstance(item, dict) and item.get("type") in ("image", "resource")
        ]
        return {
            "projectId": project_id,
            "screenId": screen_id,
            "content": content,
            "text": text,
            "imageUrls": [u for u in image_urls if u],
        }, "ok"
    if status == 200 and "error" in body:
        return body, "rpc_error"
    return body, f"error_{status}"


def get_screen_image(project_id: str, screen_id: str) -> tuple[dict, str]:
    """Fetch the image for a given Stitch screen.

    Returns (result_dict, status).
    """
    status, body = _stitch_post(
        "tools/call",
        {
            "name": "get_screen_image",
            "arguments": {"project_id": project_id, "screen_id": screen_id},
        },
    )
    if status == 200 and "result" in body:
        result = body["result"]
        if isinstance(result, dict) and result.get("isError"):
            text = _extract_text_content(body)
            return {"error": text}, "stitch_error"
        content = result.get("content", [])
        image_urls = [
            item.get("url", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "image" and item.get("url")
        ]
        return {
            "projectId": project_id,
            "screenId": screen_id,
            "content": content,
            "imageUrls": image_urls,
        }, "ok"
    if status == 200 and "error" in body:
        err = body["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        if "not found" in msg.lower() or "unknown" in msg.lower():
            return {"error": msg}, "tool_not_found"
        return {"error": msg}, "rpc_error"
    return body, f"error_{status}"
