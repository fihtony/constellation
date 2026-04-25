FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY common/ /app/common/
COPY jira/   /app/jira/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

LABEL constellation.agent_id="tracker-agent"
LABEL constellation.agent_name="Tracker Agent"
LABEL constellation.agent_role="fundamental"

CMD ["python3", "jira/app.py"]