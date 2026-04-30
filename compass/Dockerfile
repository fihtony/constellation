FROM python:3.12-slim

WORKDIR /app

# Install CA certs, tini, Node.js 22, and Claude Code CLI
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini curl \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

# Copy corporate certs early so they are in the build layer cache
RUN update-ca-certificates

COPY common/       /app/common/
COPY compass/      /app/compass/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Run as non-root user. Docker socket access is granted at runtime via group_add in docker-compose.
RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "compass/app.py"]