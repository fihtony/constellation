FROM python:3.12-slim

WORKDIR /app

# Install git (REST back-end)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini git curl nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Import any corporate CA certificates placed in certs/ (supports .crt and .pem)
COPY certs/ /usr/local/share/ca-certificates/
RUN find /usr/local/share/ca-certificates/ -name "*.pem" \
        -exec sh -c 'cp "$1" "${1%.pem}.crt"' _ {} \; \
    && update-ca-certificates

RUN npm install -g @github/copilot

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

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "scm/app.py"]
