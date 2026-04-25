FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
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
