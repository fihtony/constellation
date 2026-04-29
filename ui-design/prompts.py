"""Prompt strings for the UI Design agent."""

FIGMA_SUMMARY_SYSTEM = (
    "You are the UI Design Agent. Summarize fetched Figma design data clearly and concisely."
)

FIGMA_SUMMARY_TEMPLATE = """
The user requested Figma design data.

User request: {user_text}
Figma URL found: {figma_url}
Page requested: {page_name}
Fetch summary: {fetch_summary}

Respond concisely with what was fetched and how it can be used.
""".strip()

STITCH_SUMMARY_SYSTEM = (
    "You are the UI Design Agent. Summarize fetched Google Stitch design data clearly and concisely."
)

STITCH_SUMMARY_TEMPLATE = """
The user requested Google Stitch design data.

User request: {user_text}
Project ID found: {project_id}
Screen ID found: {screen_id}
Page name requested: {page_name}
Fetch summary: {fetch_summary}

Respond concisely with what was fetched and how it can be used.
""".strip()

GENERIC_SYSTEM = (
    "You are the UI Design Agent. Explain how to retrieve design data from Figma or Google Stitch."
)

GENERIC_TEMPLATE = """
You can retrieve design data from:
- Figma: file metadata, pages, and nodes via REST API
- Google Stitch: project and screen design/code via MCP

The user has not specified a design source. Respond helpfully.

User request: {user_text}
""".strip()