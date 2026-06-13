# Thin per-agent layer.  All shared setup (system packages, Python deps,
# agentic CLI, framework code, non-root user) lives in docker/Dockerfile.base.
# This file only adds the agent's own code, its skill, and its permissions
# file, then sets the agent's labels and CMD.

FROM constellation-base:boundary

COPY agents/scm/   /app/agents/scm/
COPY config/permissions/scm.yaml  /app/config/permissions/scm.yaml

LABEL constellation.agent_id="scm"
LABEL constellation.agent_name="SCM Agent"
LABEL constellation.agent_role="boundary"

USER appuser

CMD ["python3", "scripts/run_local.py", "scm", "--port", "8020"]

