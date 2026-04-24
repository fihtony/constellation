FROM python:3.12-slim

WORKDIR /app

COPY certs/   /app/certs/
COPY scripts/ /app/scripts/

# Copy all agent folders so init_register.py can discover registry-config.json
# and agent-card.json from each one at runtime.
COPY android/     /app/android/
COPY scm/   /app/scm/
COPY tracker/        /app/tracker/
COPY compass/      /app/compass/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

CMD ["python3", "scripts/init_register.py"]