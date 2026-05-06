# Constellation Agent Base — copilot-cli backend extension
# Adds nodejs/npm + @github/copilot CLI on top of constellation-base.
#
# Usage in agent Dockerfile:
#   FROM constellation-base-copilot-cli:latest
#   COPY <agent>/ /app/<agent>/
#   ...
#
# Build:
#   docker build -t constellation-base-copilot-cli:latest \
#     -f dockerfiles/base-copilot-cli.Dockerfile .

FROM constellation-base:latest

# nodejs/npm + Copilot CLI for copilot-cli agentic runtime backend
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @github/copilot

ENV AGENT_RUNTIME=copilot-cli
