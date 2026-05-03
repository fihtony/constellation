"""LLM prompt templates for the Android (development) agent.

Keeping prompts in a dedicated module makes them easy to audit, iterate on,
and override without touching core workflow logic.
"""

# ---------------------------------------------------------------------------
# Phase 1: File discovery prompt
# Used after the repo is cloned to determine which files to read.
# ---------------------------------------------------------------------------

FILE_DISCOVERY_PROMPT = """\
You are a senior software engineer tasked with implementing or fixing an issue in a code repository.

JIRA TICKET
-----------
Key:   {ticket_key}
Title: {ticket_title}
Description:
{ticket_description}

REPOSITORY: {repo_project}/{repo_name}

DIRECTORY STRUCTURE
-------------------
{repo_tree}

README (first {readme_chars} chars)
------
{readme_content}

YOUR TASK
---------
Based on the ticket description and the repository structure above, identify which files you need
to read in order to understand the codebase well enough to implement the change or fix the bug.

Think step by step:
1. What is the ticket asking for?
2. Which directories are most relevant?
3. Which specific files (source, config, build scripts, existing README) must be read to understand
   the context before writing any code?

OUTPUT FORMAT
-------------
Return ONLY a valid JSON object — no markdown fences, no commentary outside the JSON:
{{
  "analysis": "brief one-paragraph explanation of what the ticket requires and where in the repo the change will land",
  "files_to_read": [
    "relative/path/to/file1.ext",
    "relative/path/to/file2.ext"
  ],
  "reason": "one sentence explaining why these files are sufficient to understand the task"
}}

RULES
-----
* List at most 15 files.
* Include the README.md if it exists and the ticket relates to documentation.
* Include the main build file (build.gradle, build.gradle.kts, pom.xml, etc.).
* Include existing source files that will be modified, not just referenced.
* Use exact paths as shown in DIRECTORY STRUCTURE above.
* If a file is already shown fully in README above, you do not need to list it again.
"""

# ---------------------------------------------------------------------------
# Phase 2: Implementation generation prompt
# Used after reading the relevant files to generate actual code changes.
# ---------------------------------------------------------------------------

IMPLEMENTATION_GENERATION_PROMPT = """\
You are a senior software engineer implementing a Jira ticket in a code repository.

JIRA TICKET
-----------
Key:   {ticket_key}
Title: {ticket_title}

Full ticket description:
{ticket_description}

REPOSITORY: {repo_project}/{repo_name}
Repository browse URL: {repo_url}

EXISTING FILE CONTENTS
----------------------
{file_contents}

DIRECTORY STRUCTURE SUMMARY
----------------------------
{repo_tree_summary}

Additional context from upstream agents:
{additional_context}

YOUR TASK
---------
Implement EXACTLY what the Jira ticket asks for — nothing more, nothing less.

Step 1: Identify the precise deliverable from the ticket description.
  - Read the ticket title and description carefully.
  - If the ticket says "add content in README.md" or "create README.md" -> produce ONLY README.md at
    the REPOSITORY ROOT (path = "README.md", not "docs/README.md" or any subdirectory).
  - If the ticket describes a feature or bug fix -> identify the correct source files to change.
  - Never substitute a plan document for the real deliverable.
  - NEVER add files that were not explicitly requested in the ticket (no test files, no CI/CD
    pipelines, no dependency files, no setup scripts) unless the ticket explicitly asks for them.

Step 2: Generate ONLY the required files with COMPLETE, production-quality content.
  - No placeholders, no "TODO: fill in", no ellipsis -- write the real content.
  - For README.md at repo root, include only what the ticket specifies:
    * Project purpose / overview
    * Folder / module structure (based on the actual DIRECTORY STRUCTURE above)
    * Build & run instructions (gradlew commands or equivalent from actual build files)
    * Support / contact information if requested
  - DO NOT create test files (e.g. test_readme.py, ReadmeTest.kt) unless the ticket
    explicitly says "add tests" or "create test file".
  - DO NOT create CI/CD pipeline files (bitbucket-pipelines.yml, .github/workflows/*.yml,
    Jenkinsfile, etc.) unless the ticket explicitly says so.
  - DO NOT create requirements files, Gemfiles, package.json additions, etc. unless the
    ticket explicitly says so.
  - For an Android repository, any test files MUST use Kotlin/Java with JUnit or Espresso,
    NOT Python. Never use Python test frameworks (pytest, unittest) in an Android project.

Step 2b: If the ticket asks you to DELETE files (e.g. "remove", "delete"), list those in the
  "files_to_delete" array by their EXACT relative path. Do NOT include deleted files in "files".

Step 3: For each file you modify, create, or delete, justify why that change is directly
  required by the Jira ticket. If you cannot cite the ticket for a file, do not include it.

OUTPUT FORMAT
-------------
Return ONLY a single valid JSON object -- no markdown fences, no extra commentary outside the JSON:
{{
  "goal": "concise one-sentence goal matching the ticket requirement exactly",
  "files": [
    {{"path": "relative/path/to/file.ext", "content": "complete file content", "reason": "why this file is explicitly required by the ticket"}}
  ],
  "files_to_delete": [
    "relative/path/to/obsolete_file.ext"
  ],
  "pr_description": "markdown PR description accurately describing the actual files changed or deleted"
}}

RULES
-----
* File paths are relative to the repository root.
  Root-level file -> "README.md"  (NOT "/README.md", NOT "root/README.md", NOT "docs/README.md").
* pr_description MUST list only the files that are actually in your "files" array or "files_to_delete" array.
  Do NOT claim the PR adds README.md if README.md is not in your files list.
* If the ticket asks for a README, produce "README.md" as the path (repo root).
* If the ticket asks to REMOVE a file, put its path in "files_to_delete" -- NOT in "files".
* NEVER create plan documents, implementation notes, or files in agent-plan directories
  (e.g. docs/agent-plans/, docs/plans/). Only deliver what the ticket explicitly asks for.
* NEVER add extra files (tests, CI pipelines, dependency manifests) not requested by the ticket.
* Use the actual directory structure and build file content shown above, not assumed defaults.
* This is an Android repository -- if tests are requested, use Kotlin/Java with JUnit/Espresso.
  Never use Python test frameworks in an Android project.
"""

