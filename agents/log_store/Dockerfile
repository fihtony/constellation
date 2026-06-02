FROM python:3.12-slim
WORKDIR /app
COPY agents/log_store/ /app/agents/log_store/
RUN pip install -e .
CMD ["python", "-m", "agents.log_store.agent"]