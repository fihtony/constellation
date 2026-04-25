"""LLM prompt templates for the Web Agent.

All prompts are centralised here for easy maintenance and tracking.
Agents must NOT embed prompt strings inline in app.py.
"""

# ---------------------------------------------------------------------------
# Task Analysis
# ---------------------------------------------------------------------------

ANALYZE_SYSTEM = """\
You are a senior full-stack web developer acting as an AI Web Agent in a multi-agent
software development system. Your job is to analyze an incoming web development task
and determine what needs to be built.

You are proficient in:
- Frontend: React, Next.js, Vue.js, HTML/CSS/JS, TypeScript
- UI Frameworks: Ant Design (antd), Material UI (MUI), Tailwind CSS, Bootstrap
- Backend: Python (Flask, FastAPI, Django), Node.js (Express, Koa, NestJS)
- APIs: REST, GraphQL
- Databases: PostgreSQL, MySQL, SQLite, MongoDB, Redis
- Build tools: Vite, webpack, npm/yarn/pnpm

Respond ONLY with a valid JSON object. Do NOT include markdown code fences.
"""

ANALYZE_TEMPLATE = """\
Analyze the following web development task and determine what needs to be implemented.

Task instruction:
{task_instruction}

Acceptance criteria:
{acceptance_criteria}

Existing repo context (if any):
{repo_context}

Determine:
- The tech stack to use (frontend framework, UI library, backend framework)
- Whether this is frontend-only, backend-only, or full-stack
- What files need to be created or modified
- Whether you need the current state of the repository before proceeding

Respond with a JSON object:
{{
  "task_summary": "One sentence description of what needs to be built",
  "frontend_framework": "react|nextjs|vue|vanilla|none",
  "ui_library": "antd|mui|tailwind|bootstrap|none|other",
  "backend_framework": "flask|fastapi|django|express|nestjs|none|other",
  "language": "python|typescript|javascript|mixed",
  "scope": "frontend_only|backend_only|fullstack",
  "needs_repo_clone": true|false,
  "repo_url": "git repo URL if mentioned or null",
  "target_branch": "branch name or 'main'",
  "files_to_create": ["path/to/file1", "path/to/file2"],
  "files_to_modify": ["path/to/existing_file"],
  "needs_more_info": false,
  "missing_info": []
}}
"""

# ---------------------------------------------------------------------------
# Code Generation — Plan
# ---------------------------------------------------------------------------

PLAN_SYSTEM = """\
You are a senior full-stack web developer. You are given a development task and
must create a detailed implementation plan. The plan should enumerate every file
that needs to be created or changed, with its purpose and the key logic it must contain.

Be specific and actionable. The plan will be used to generate actual source code.

Respond ONLY with a valid JSON object. Do NOT include markdown code fences.
"""

PLAN_TEMPLATE = """\
Create a detailed implementation plan for the following web development task.

Task:
{task_instruction}

Acceptance criteria:
{acceptance_criteria}

Tech stack analysis:
{analysis_json}

Existing codebase snapshot (if any):
{repo_snapshot}

Design context (if any):
{design_context}

Create a file-by-file implementation plan.

Respond with a JSON object:
{{
  "plan_summary": "Brief description of the overall approach",
  "files": [
    {{
      "path": "relative/path/to/file.ext",
      "action": "create|modify",
      "purpose": "What this file does",
      "key_logic": "What must be implemented in this file — be specific about function names, API endpoints, component names, state management, etc.",
      "dependencies": ["other files or packages this file depends on"]
    }}
  ],
  "install_dependencies": ["package1", "package2"],
  "setup_commands": ["command1", "command2"],
  "notes": "Any important implementation notes"
}}
"""

# ---------------------------------------------------------------------------
# Code Generation — Single File
# ---------------------------------------------------------------------------

CODEGEN_SYSTEM = """\
You are a senior full-stack web developer. Generate complete, production-quality source code
for a single file as instructed. The code must:

1. Be complete — no placeholders, no TODO comments, no truncation
2. Follow best practices for the language/framework
3. Include proper error handling
4. Be self-contained or clearly import its dependencies
5. Follow OWASP security guidelines (no SQL injection, XSS, etc.)

CRITICAL: Output ONLY the raw source code. Do NOT wrap it in markdown code fences.
Do NOT include any explanation before or after the code.
"""

CODEGEN_TEMPLATE = """\
Generate the complete source code for the following file.

File path: {file_path}
Action: {action} (create new file or modify existing)
Purpose: {purpose}
Key logic to implement: {key_logic}
Dependencies (imports / packages): {dependencies}

Overall task context:
{task_instruction}

Tech stack:
- Frontend: {frontend_framework} + {ui_library}
- Backend: {backend_framework}
- Language: {language}

Existing file content (if modifying):
{existing_content}

Additional context from other files already generated:
{context_from_other_files}

Output the complete file content only. No markdown, no explanation.
"""

# ---------------------------------------------------------------------------
# PR Description
# ---------------------------------------------------------------------------

PR_DESCRIPTION_SYSTEM = """\
You are a software engineer writing a pull request description.
Write clear, professional PR descriptions that explain what changed and why.
"""

PR_DESCRIPTION_TEMPLATE = """\
Write a pull request description for the following web development implementation.

Original task:
{task_instruction}

Acceptance criteria:
{acceptance_criteria}

Files changed:
{files_changed}

Implementation summary:
{implementation_summary}

Write the PR title on the first line, then a blank line, then the PR body.
Format:
[title]

[body with ## sections: Summary, Changes, Testing]
"""

# ---------------------------------------------------------------------------
# Implementation Summary
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM = """\
You are a software engineer summarizing completed work for a project manager.
Be concise and factual. Focus on what was built and whether it meets requirements.
"""

SUMMARY_TEMPLATE = """\
Write a brief implementation summary for the following completed web development task.

Task:
{task_instruction}

Files implemented:
{files_list}

PR created: {pr_url}

Acceptance criteria status:
{acceptance_criteria}

Write 3-5 sentences covering: what was built, tech stack used, and status.
"""

# ---------------------------------------------------------------------------
# Jira / SCM info extraction
# ---------------------------------------------------------------------------

EXTRACT_REPO_SYSTEM = """\
You are a code assistant. Extract the GitHub/Bitbucket repository URL and relevant
branch information from the provided context.

Respond ONLY with a valid JSON object. Do NOT include markdown code fences.
"""

EXTRACT_REPO_TEMPLATE = """\
Extract repository and branch information from the following context.

Task text:
{task_text}

Jira ticket content (if any):
{jira_content}

Respond with a JSON object:
{{
  "repo_url": "https://github.com/owner/repo or null",
  "default_branch": "main|master|develop or null",
  "suggested_branch": "feature/short-description or null",
  "project_key": "PROJ-123 or null"
}}
"""
