FROM python:3.12-slim

WORKDIR /app

COPY scripts/ /app/scripts/
COPY common/  /app/common/

# Copy all agent folders so init_register.py can discover registry-config.json
# and agent-card.json from each one at runtime.
COPY jira/        /app/jira/
COPY scm/         /app/scm/
COPY compass/     /app/compass/
COPY office/      /app/office/
COPY team-lead/   /app/team_lead/
COPY ui-design/   /app/ui-design/
COPY web/         /app/web/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

CMD ["python3", "scripts/init_register.py"]