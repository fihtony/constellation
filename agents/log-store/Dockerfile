FROM python:3.12-slim
WORKDIR /app
COPY agents/log-store/ /app/
RUN pip install -e .
CMD ["python", "-m", "agents.log_store.agent"]