FROM python:3.12-slim

WORKDIR /app

# Install Node.js 22 (required for GitHub Copilot CLI) + git for SCM operations
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git curl \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @github/copilot \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir flask pytest requests

COPY common/  /app/common/
COPY web/     /app/web/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Required Docker labels (see copilot-instructions.md)
LABEL constellation.agent_id="web-agent"
LABEL constellation.agent_name="Web Agent"
LABEL constellation.agent_role="execution"

CMD ["python3", "web/app.py"]
