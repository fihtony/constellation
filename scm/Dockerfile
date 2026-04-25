FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY common/  /app/common/
COPY scm/     /app/scm/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

LABEL constellation.agent_id="scm-agent"
LABEL constellation.agent_name="SCM Agent"
LABEL constellation.agent_role="integration"

CMD ["python3", "scm/app.py"]
