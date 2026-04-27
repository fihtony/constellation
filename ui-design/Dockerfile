FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @github/copilot \
    && rm -rf /var/lib/apt/lists/*

COPY common/     /app/common/
COPY ui-design/  /app/ui-design/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

LABEL constellation.agent_id="ui-design-agent"
LABEL constellation.agent_name="UI Design Agent"
LABEL constellation.agent_role="integration"

CMD ["python3", "ui-design/app.py"]
