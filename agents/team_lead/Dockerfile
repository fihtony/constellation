# Thin per-agent layer.  All shared setup (system packages, Python deps,
# agentic CLI, framework code, non-root user) lives in docker/Dockerfile.base.
# This file only adds the agent's own code, its skill, and its permissions
# file, then sets the agent's labels and CMD.

ARG AGENT_RUNTIME=claude-code
FROM constellation-base:agentic-${AGENT_RUNTIME}

COPY agents/team_lead/   /app/agents/team_lead/
COPY config/permissions/team-lead.yaml  /app/config/permissions/team-lead.yaml
COPY config/permissions/web-dev.yaml  /app/config/permissions/web-dev.yaml
COPY config/permissions/code-review.yaml  /app/config/permissions/code-review.yaml

LABEL constellation.agent_id="team_lead"
LABEL constellation.agent_name="Team Lead Agent"
LABEL constellation.agent_role="fundamental"

USER appuser

CMD ["python3", "scripts/run_local.py", "team_lead", "--port", "8030"]

