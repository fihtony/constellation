FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY common/      /app/common/
COPY team-lead/   /app/team-lead/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Required Docker labels (see copilot-instructions.md)
LABEL constellation.agent_id="team-lead-agent"
LABEL constellation.agent_name="Team Lead Agent"
LABEL constellation.agent_role="fundamental"

CMD ["python3", "team-lead/app.py"]
