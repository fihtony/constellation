# Thin per-agent layer.  All shared setup (system packages, Python deps,
# agentic CLI, framework code, non-root user) lives in docker/Dockerfile.base.
# This file only adds the agent's own code, its skill, and its permissions
# file, then sets the agent's labels and CMD.

ARG AGENT_RUNTIME=claude-code
FROM constellation-base:${AGENT_RUNTIME}

COPY agents/office/   /app/agents/office/
COPY skills/office-generic-methodology/  /app/skills/office-generic-methodology/
COPY config/permissions/office.yaml  /app/config/permissions/office.yaml

LABEL constellation.agent_id="office"
LABEL constellation.agent_name="Office Agent"
LABEL constellation.agent_role="per-task"

USER appuser

CMD ["python3", "scripts/run_local.py", "office", "--port", "8060"]

