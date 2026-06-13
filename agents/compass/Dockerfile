# Thin per-agent layer.  All shared setup (system packages, Python deps,
# agentic CLI, framework code, non-root user) lives in docker/Dockerfile.base.
# This file only adds the agent's own code, its skill, and its permissions
# file, then sets the agent's labels and CMD.

ARG AGENT_RUNTIME=claude-code
FROM constellation-base:${AGENT_RUNTIME}

COPY agents/compass/   /app/agents/compass/
COPY agents/log_store/  /app/agents/log_store/
COPY config/permissions/compass.yaml  /app/config/permissions/compass.yaml
COPY config/permissions/office.yaml  /app/config/permissions/office.yaml

LABEL constellation.agent_id="compass"
LABEL constellation.agent_name="Compass Agent"
LABEL constellation.agent_role="fundamental"

USER appuser

CMD ["python3", "scripts/run_local.py", "compass", "--port", "8000"]

