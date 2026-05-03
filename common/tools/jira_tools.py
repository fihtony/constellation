"""Jira tools for agentic runtimes.

Provides tools that allow an agentic runtime to interact with the Jira Agent
via A2A HTTP calls.  Import this module to register all Jira tools.
"""

from __future__ import annotations

import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen

from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import register_tool

_REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")
_ACK_TIMEOUT = int(os.environ.get("A2A_ACK_TIMEOUT_SECONDS", "15"))


def _discover_jira_url(capability: str) -> str | None:
    try:
        req = Request(
            f"{_REGISTRY_URL}/query?capability={capability}",
            headers={"Accept": "application/json"},
        )
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        agents = data.get("agents") or []
        for agent in agents:
            instances = agent.get("instances") or []
            for inst in instances:
                url = inst.get("url") or agent.get("baseUrl")
                if url:
                    return url.rstrip("/")
        return None
    except Exception:  # noqa: BLE001
        return None


def _a2a_send(agent_url: str, capability: str, params: dict) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": "tool-call",
        "method": "message:send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": json.dumps(params)}],
            },
            "metadata": {"capability": capability, **params},
        },
    }
    req = Request(
        f"{agent_url}/message:send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=_ACK_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


class JiraGetTicketTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_get_ticket",
            description="Fetch Jira ticket details by key (e.g. PROJ-123).",
            input_schema={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Jira ticket key, e.g. PROJ-123",
                    }
                },
                "required": ["key"],
            },
        )

    def execute(self, args: dict) -> dict:
        key = args.get("key", "").strip()
        if not key:
            return self.error("Missing required argument: key")
        url = _discover_jira_url("jira.ticket.fetch")
        if not url:
            return self.error("Jira Agent is not available (capability jira.ticket.fetch not found).")
        try:
            result = _a2a_send(url, "jira.ticket.fetch", {"ticketKey": key})
            return self.ok(json.dumps(result, ensure_ascii=False, indent=2))
        except (URLError, OSError) as exc:
            return self.error(f"Failed to fetch Jira ticket {key}: {exc}")


class JiraAddCommentTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_add_comment",
            description="Add a comment to a Jira ticket.",
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Jira ticket key"},
                    "comment": {"type": "string", "description": "Comment text to add"},
                },
                "required": ["key", "comment"],
            },
        )

    def execute(self, args: dict) -> dict:
        key = args.get("key", "").strip()
        comment = args.get("comment", "").strip()
        if not key or not comment:
            return self.error("Both 'key' and 'comment' are required.")
        url = _discover_jira_url("jira.ticket.comment")
        if not url:
            return self.error("Jira Agent is not available (capability jira.ticket.comment not found).")
        try:
            result = _a2a_send(url, "jira.ticket.comment", {"ticketKey": key, "comment": comment})
            return self.ok(json.dumps(result, ensure_ascii=False))
        except (URLError, OSError) as exc:
            return self.error(f"Failed to add comment to {key}: {exc}")


register_tool(JiraGetTicketTool())
register_tool(JiraAddCommentTool())
