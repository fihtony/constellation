"""Prompt strings for the SCM agent."""

DISPATCH_SYSTEM = (
    "You are the SCM Agent. Analyze SCM-oriented user intent and summarize the next safe repository action."
)

DISPATCH_TEMPLATE = """
Provider: {provider_name}
User request: {user_text}
Message metadata:
{metadata}

Return a concise analysis of the intended SCM action, any ambiguity, and the recommended next step.
""".strip()