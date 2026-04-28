FROM python:3.12-slim

WORKDIR /app

# Install Node.js 22 (required for GitHub Copilot CLI)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @github/copilot \
    && rm -rf /var/lib/apt/lists/*

COPY common/      /app/common/
COPY team-lead/   /app/team_lead/
# Create hyphenated alias so docker-compose command path still works
RUN ln -s /app/team_lead /app/team-lead

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Required Docker labels (see copilot-instructions.md)
LABEL constellation.agent_id="team-lead-agent"
LABEL constellation.agent_name="Team Lead Agent"
LABEL constellation.agent_role="fundamental"

# Run as non-root user.
RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python3", "team-lead/app.py"]
