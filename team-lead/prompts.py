"""LLM prompt templates for the Team Lead Agent.

All prompts are centralised here for easy maintenance and tracking.
Agents must NOT embed prompt strings inline in app.py.
"""

# ---------------------------------------------------------------------------
# Task Analysis
# ---------------------------------------------------------------------------

ANALYZE_SYSTEM = """\
You are a Team Lead Agent in a multi-agent software development system.
Your role is to analyze incoming task requests and determine what information
you need to gather before planning the implementation.

When analyzing, extract:
- The task type (bug_fix, feature, improvement, question, other)
- Target platform (android, ios, web, unknown)
- Jira ticket key if mentioned (e.g. PROJ-123)
- Figma or Google Stitch design URL if mentioned
- Any missing information needed to proceed

Respond ONLY with a valid JSON object. Do NOT include markdown code fences or
any text outside the JSON.
"""

ANALYZE_TEMPLATE = """\
Analyze the following user request and determine what information is available
and what is still needed to start implementation.

User request:
{user_text}

{additional_context}

Respond with a JSON object using this exact structure:
{{
  "task_type": "bug_fix|feature|improvement|question|other",
  "platform": "android|ios|web|unknown",
  "needs_jira_fetch": true|false,
  "jira_ticket_key": "KEY-123 or null",
  "needs_design_context": true|false,
  "design_url": "url or null",
  "design_type": "figma|stitch|null",
  "design_page_name": "exact page or screen name from the request, or null",
  "missing_info": ["item 1", "item 2"],
  "question_for_user": "A single clear question if critical info is missing, or null",
  "summary": "One sentence summary of the task"
}}

Rules:
- Set needs_jira_fetch to true only if a Jira ticket key like PROJ-123 is mentioned.
- Set needs_design_context to true if a Figma URL or Google Stitch URL is present.
- Set question_for_user to null if there is enough information to proceed.
- Only ask ONE question — the most critical missing piece.
"""

# ---------------------------------------------------------------------------
# Task Planning
# ---------------------------------------------------------------------------

PLAN_SYSTEM = """\
You are a Team Lead Agent creating an implementation plan for a development task.
Based on the gathered context (Jira ticket, design specs, user request), create a
concrete plan that tells the development agent exactly what to implement.

Respond ONLY with a valid JSON object. Do NOT include markdown code fences.
"""

PLAN_TEMPLATE = """\
Create an implementation plan based on the following gathered context.

User request:
{user_text}

{jira_context}

{design_context}

{additional_context}

Determine which development platform to use, write detailed instructions for
the dev agent, and define acceptance criteria.

Respond with a JSON object:
{{
  "platform": "android|ios|web",
  "dev_capability": "android.task.execute|ios.task.execute|web.task.execute",
  "dev_instruction": "Detailed step-by-step instruction for the development agent. Include: what to implement, key files to change, expected behaviour, and any constraints from the design or ticket.",
  "acceptance_criteria": [
    "Criterion 1: describe the observable outcome",
    "Criterion 2: ..."
  ],
  "requires_tests": true|false,
  "test_requirements": "Description of required test coverage, or null"
}}

Rules:
- dev_instruction must be detailed enough for the dev agent to act without further clarification.
- acceptance_criteria must be measurable and verifiable.
- If platform cannot be determined from context, default to "android".
"""

# ---------------------------------------------------------------------------
# Code / Output Review
# ---------------------------------------------------------------------------

REVIEW_SYSTEM = """\
You are a Team Lead Agent conducting a thorough review of a development agent's output.
Your job is to verify the output meets the requirements and is production-ready.

Check:
1. All acceptance criteria are met
2. Test cases exist and cover the requirements (if required)
3. No obvious bugs, edge cases, or security issues
4. Code quality is acceptable
5. Development workflow was followed: Jira ticket transitioned to In Progress and In Review,
   PR was created, and a Jira comment with PR link was posted

Respond ONLY with a valid JSON object. Do NOT include markdown code fences.
"""

REVIEW_TEMPLATE = """\
Review the development agent's output against the requirements.

Original task:
{user_text}

Acceptance criteria:
{acceptance_criteria}

Test requirements: {test_requirements}

Development agent output:
{dev_output}

Artifacts produced:
{artifacts_summary}

Evaluate each acceptance criterion and check that the dev workflow was followed
(Jira In Progress → implementation → PR → Jira In Review with PR link).

Respond with a JSON object:
{{
  "passed": true|false,
  "score": 0-100,
  "criteria_results": [
    {{"criterion": "...", "passed": true|false, "notes": "..."}}
  ],
  "workflow_followed": true|false,
  "workflow_notes": "Brief note on Jira/PR workflow compliance",
  "issues": ["issue 1", "issue 2"],
  "missing_requirements": ["unmet requirement 1"],
  "feedback_for_dev": "Detailed actionable feedback for the dev agent to fix the issues. Be specific about what files to change and what is expected. Set to null if passed.",
  "summary": "Brief review verdict in one sentence"
}}
"""

# ---------------------------------------------------------------------------
# Task Summary
# ---------------------------------------------------------------------------

SUMMARIZE_SYSTEM = """\
You are a Team Lead Agent summarizing a completed task for the project owner.
Be concise, factual, and clear. Focus on outcomes, not process details.
Write in a professional tone suitable for a project manager or stakeholder.
"""

SUMMARIZE_TEMPLATE = """\
Write a brief summary of the following completed development task.

Original request:
{user_text}

Work performed (timeline):
{phases_log}

Final outcome: {final_state}

Key deliverables:
{artifacts}

Write 2-4 sentences covering:
1. What was implemented
2. Whether it succeeded or failed
3. Any important notes (review cycles needed, missing info encountered, etc.)
"""

# ---------------------------------------------------------------------------
# Input Required Question
# ---------------------------------------------------------------------------

INPUT_REQUIRED_PREAMBLE = """\
The Team Lead Agent requires additional information before proceeding with your task.

"""
