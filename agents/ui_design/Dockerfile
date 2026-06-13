# Thin per-agent layer.  All shared setup (system packages, Python deps,
# agentic CLI, framework code, non-root user) lives in docker/Dockerfile.base.
# This file only adds the agent's own code, its skill, and its permissions
# file, then sets the agent's labels and CMD.

FROM constellation-base:boundary

COPY agents/ui_design/   /app/agents/ui_design/
COPY config/permissions/ui-design.yaml  /app/config/permissions/ui-design.yaml

LABEL constellation.agent_id="ui_design"
LABEL constellation.agent_name="UI Design Agent"
LABEL constellation.agent_role="boundary"

USER appuser

CMD ["python3", "scripts/run_local.py", "ui_design", "--port", "8040"]

