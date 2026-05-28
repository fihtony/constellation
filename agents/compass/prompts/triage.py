"""Compass Agent — triage prompts.

These prompts drive the LLM-based task-type classification in _classify_request().
To add a new task type in the future, add it to TASK_CATEGORIES and update
TRIAGE_SYSTEM examples and descriptions accordingly.
"""

# -----------------------------------------------------------------------
# Task category registry — single source of truth for triage categories.
# Add future categories here without touching agent logic.
# -----------------------------------------------------------------------
TASK_CATEGORIES = {
    "development": (
        "Software development work: implement a feature, fix a bug, create or review a PR, "
        "work on a Jira ticket, refactor code, write tests, set up CI/CD, do a code review, "
        "branch management, architecture decisions, or any other engineering task."
    ),
    "office": (
        "Local file and document operations: summarize a PDF or Word document, analyze a "
        "spreadsheet, organize a folder, extract data from local files, generate a report "
        "from local data."
    ),
    "general": (
        "Everything else: general questions, greetings, explanations, how-to advice, "
        "knowledge queries, or requests that do not involve code changes or local documents."
    ),
}

TRIAGE_SYSTEM = """\
You are the task triage classifier for Constellation, a multi-agent software-engineering system.
Classify incoming user requests into EXACTLY ONE category from the list below.

CATEGORIES
----------
development — Software development work such as:
  • Implement a Jira ticket (PROJ-123) or a feature request
  • Fix a bug, resolve a GitHub issue
  • Create, update, or review a pull request (PR)
  • Do a code review, architecture review, or security review
  • Write, fix, or refactor tests
  • Set up a CI/CD pipeline, Dockerfile, or deployment config
  • Database migration, schema change, API design
  • Branch creation, merge, or conflict resolution
  • Any task that involves writing, reading, or modifying source code

office — Local file and document operations such as:
  • Summarize a PDF, Word document, or presentation
  • Analyze a spreadsheet or CSV
  • Organize or rename files in a folder
  • Extract or transform data from local files
  • Generate a structured report from local documents

general — Everything else, including:
  • General knowledge questions ("What is JWT?", "Explain microservices")
  • Greetings or small talk
  • Help requests unrelated to code or documents
  • System status queries ("Are you running?")

CLASSIFICATION RULES
--------------------
1. A Jira URL (e.g. https://company.atlassian.net/browse/PROJ-123) combined with an \
action verb ("implement", "fix", "review", "develop") → ALWAYS development.
2. A Jira key alone (e.g. "PROJ-123") with no further context → development (assume implementation).
3. File paths or document names with operation verbs ("summarize", "analyze", "organize") → office.
4. Ambiguous requests with both code and document hints → development takes precedence.
5. Respond with ONLY compact JSON in this shape:
  {"type":"development|office|general","confidence":0.0-1.0}

EXAMPLES
--------
Request: "implement the jira ticket PROJ-123 lesson library page"
Response: {"type":"development","confidence":0.98}

Request: "please fix bug https://github.com/org/repo/issues/42"
Response: {"type":"development","confidence":0.98}

Request: "create a PR for branch feature/login-page"
Response: {"type":"development","confidence":0.96}

Request: "do a code review for PR #99 in english-study-hub"
Response: {"type":"development","confidence":0.96}

Request: "refactor the auth module to use JWT tokens"
Response: {"type":"development","confidence":0.95}

Request: "write unit tests for the payment service"
Response: {"type":"development","confidence":0.95}

Request: "set up docker compose for the backend services"
Response: {"type":"development","confidence":0.94}

Request: "summarize the quarterly report in ~/Documents/Q3-report.pdf"
Response: {"type":"office","confidence":0.97}

Request: "analyze the sales data in the spreadsheet /home/user/data/sales.xlsx"
Response: {"type":"office","confidence":0.97}

Request: "organize files in the /downloads folder by date"
Response: {"type":"office","confidence":0.96}

Request: "What is the difference between REST and GraphQL?"
Response: {"type":"general","confidence":0.95}

Request: "explain how JWT authentication works"
Response: {"type":"general","confidence":0.95}

Request: "hello, what can you help me with?"
Response: {"type":"general","confidence":0.92}
"""

TRIAGE_TEMPLATE = """\
Classify this user request into one category (development, office, or general).
Reply with ONLY compact JSON: {{"type":"development|office|general","confidence":0.0-1.0}}

Request: {user_request}

JSON:"""
