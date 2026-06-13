# Thin per-agent layer.  All shared setup (system packages, Python deps,
# agentic CLI, framework code, non-root user) lives in docker/Dockerfile.base.
# This file only adds the agent's own code, its skill, and its permissions
# file, then sets the agent's labels and CMD.

ARG AGENT_RUNTIME=claude-code
FROM constellation-base:agentic-${AGENT_RUNTIME}

COPY agents/code_review/   /app/agents/code_review/
COPY skills/code-review/  /app/skills/code-review/
COPY config/permissions/code-review.yaml  /app/config/permissions/code-review.yaml

LABEL constellation.agent_id="code_review"
LABEL constellation.agent_name="Code Review Agent"
LABEL constellation.agent_role="per-task"

USER appuser

CMD ["python3", "scripts/run_local.py", "code_review"]

