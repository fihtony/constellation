"""LLM prompt templates for the Compass Agent.

Routing and summarization prompts are now embedded in the modular prompt files
under compass/prompts/system/ and compass/prompts/tasks/. Only prompts that are
used by specific HTTP-level handlers remain here.
"""

OFFICE_REPLY_SYSTEM = """\
You are the Compass router interpreting a user's reply to an office-task routing question.

Return a structured decision only. Do not invent paths or permissions.
Return JSON only.
"""

OFFICE_REPLY_TEMPLATE = """\
Original request:
{original_request}

Current step:
{awaiting_step}

Current question shown to user:
{current_question}

Current office context:
{office_context}

User reply:
{user_reply}

Return JSON using this exact structure:
{{
  "action": "workspace|inplace|approve|deny|unclear",
  "clarification_question": "question or null",
  "reasoning": "brief explanation"
}}

Rules:
- If awaiting_step is output_mode, choose workspace or inplace.
- If awaiting_step is confirm_write, choose approve or deny.
- If the reply is ambiguous, return unclear and ask a short clarification question.
"""