FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (tini for signal handling, git for SCM tools)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini git curl nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Import any corporate CA certificates placed in certs/ (supports .crt and .pem)
COPY certs/ /usr/local/share/ca-certificates/
RUN find /usr/local/share/ca-certificates/ -name "*.pem" \
        -exec sh -c 'cp "$1" "${1%.pem}.crt"' _ {} \; \
    && update-ca-certificates

RUN npm install -g @github/copilot

COPY common/      /app/common/
COPY team-lead/   /app/team_lead/
COPY .github/skills/ /app/.github/skills/
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

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "team-lead/app.py"]
