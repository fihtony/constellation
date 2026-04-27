FROM node:22-slim

WORKDIR /workspace

# Install system dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        curl \
        python3 \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub Copilot CLI globally
RUN npm install -g @github/copilot

# Copy test script
COPY copilot-cli-test/test_agent.py /workspace/test_agent.py

# Auth: COPILOT_GITHUB_TOKEN is the recommended method for containers.
# It must be passed at runtime: docker run -e COPILOT_GITHUB_TOKEN=... ...
# The token must be a fine-grained PAT with "Copilot Requests" permission.

ENV PYTHONUNBUFFERED=1

CMD ["python3", "/workspace/test_agent.py"]
