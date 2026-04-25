FROM python:3.12-slim

WORKDIR /app

COPY common/   /app/common/
COPY registry/ /app/registry/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

CMD ["python3", "registry/app.py"]