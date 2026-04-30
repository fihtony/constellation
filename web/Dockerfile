FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (tini for signal handling, git for SCM tools)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates tini git curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir flask pytest requests

COPY common/  /app/common/
COPY web/     /app/web/
COPY .github/skills/ /app/.github/skills/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Required Docker labels (see copilot-instructions.md)
LABEL constellation.agent_id="web-agent"
LABEL constellation.agent_name="Web Agent"
LABEL constellation.agent_role="execution"

# Run as non-root user.
RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "web/app.py"]
