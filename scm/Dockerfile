FROM python:3.12-slim

WORKDIR /app

# Install git (REST back-end) + Node.js 22 + npx (MCP back-end, MCP_RUNNER=npx)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git curl \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @github/copilot \
    && rm -rf /var/lib/apt/lists/*

COPY common/  /app/common/
COPY scm/     /app/scm/
# Include skills so scm/app.py can load _read_skill_guide() at runtime
COPY .github/skills/ /app/.github/skills/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    SCM_GITHUB_BACKEND=rest \
    MCP_RUNNER=auto

LABEL constellation.agent_id="scm-agent"
LABEL constellation.agent_name="SCM Agent"
LABEL constellation.agent_role="integration"

# Run as non-root user.
RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python3", "scm/app.py"]
