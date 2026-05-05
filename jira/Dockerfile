FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini curl nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Import any corporate CA certificates placed in certs/ (supports .crt and .pem)
COPY certs/ /usr/local/share/ca-certificates/
RUN find /usr/local/share/ca-certificates/ -name "*.pem" \
        -exec sh -c 'cp "$1" "${1%.pem}.crt"' _ {} \; \
    && update-ca-certificates

RUN npm install -g @github/copilot

COPY common/ /app/common/
COPY jira/   /app/jira/
COPY .github/skills/ /app/.github/skills/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

LABEL constellation.agent_id="tracker-agent"
LABEL constellation.agent_name="Tracker Agent"
LABEL constellation.agent_role="fundamental"

# Run as non-root user.
RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "jira/app.py"]