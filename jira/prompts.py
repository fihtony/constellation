"""Prompt strings for the Jira agent."""

SUMMARY_SYSTEM = (
    "You are the Jira Agent in a Constellation multi-agent software delivery system. "
    "Summarize Jira context for downstream engineering agents in clear operator-facing prose."
)

SUMMARY_TEMPLATE = """
Operational skill guide:
{skill_guide}

User request:
{user_text}

Detected ticket key: {ticket_key}
Ticket browse URL: {browse_url}
Fetch status: {fetch_status}
Issue payload:
{issue_payload}

Return a concise operator-facing summary with these sections:
1. Ticket
2. What matters
3. Recommended next engineering step
""".strip()