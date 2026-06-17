#!/usr/bin/env bash
# ============================================================================
# Constellation — build the base image(s)
# ============================================================================
# The base image is the single place we install system packages, the
# union of every agent's Python dependencies, the agentic CLI, and
# the shared framework code.  Every per-agent Dockerfile (compass,
# office, jira, …) FROMs one of the base images tagged below, so
# the base must exist in the local Docker registry before
# ``docker compose build`` (or ``docker compose up --build``) will
# succeed.
#
# Two base flavors are produced, depending on whether the agentic
# CLI is needed:
#
#   constellation-base:agentic-<runtime>   (full; ~1.2 GB)
#       nodejs + npm + the agentic CLI (claude-code / copilot-cli /
#       codex-cli / connect-agent).  Used by every agent that makes
#       local LLM calls (compass, code_review, log_store, office,
#       team_lead, web_dev, and the registry / init-register
#       services).
#
#   constellation-base:boundary             (slim; ~250 MB lighter)
#       No nodejs, no npm, no agentic CLI.  Used by the three
#       boundary agents (jira, scm, ui_design) which only speak
#       the A2A protocol and proxy to the external system — they
#       do not run an LLM locally.
#
# USAGE
# -----
#   ./scripts/build_base.sh                    # build the default
#                                              # (claude-code agentic
#                                              # + boundary slim)
#   ./scripts/build_base.sh copilot-cli        # agentic variant for
#                                              # a different runtime
#   ./scripts/build_base.sh all                # build every
#                                              # supported agentic
#                                              # variant + boundary
#
# RUNTIME / FLAVOR ENVS
# ---------------------
#   AGENT_RUNTIME=copilot-cli ./scripts/build_base.sh all
#       The runtime arg is also read by the per-agent Dockerfiles
#       (via the ``AGENT_RUNTIME`` build arg in docker-compose-v2.yml)
#       to pick the right base tag.  When you build with one value
#       here, the per-agent compose build must use the SAME value,
#       otherwise the FROM in the per-agent Dockerfile will not
#       resolve.
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

RUNTIME="${1:-${AGENT_RUNTIME:-claude-code}}"

AGENTIC_VARIANTS=("claude-code" "copilot-cli" "codex-cli" "connect-agent")

build_agentic() {
    local runtime="$1"
    local tag="constellation-base:agentic-${runtime}"
    echo ">>> building ${tag}"
    docker buildx build \
        --load \
        --build-arg "BASE_FLAVOR=agentic" \
        --build-arg "AGENT_RUNTIME=${runtime}" \
        -t "${tag}" \
        -f docker/Dockerfile.base \
        .
}

build_boundary() {
    local tag="constellation-base:boundary"
    echo ">>> building ${tag}"
    docker buildx build \
        --load \
        --build-arg "BASE_FLAVOR=boundary" \
        --build-arg "AGENT_RUNTIME=none" \
        -t "${tag}" \
        -f docker/Dockerfile.base \
        .
}

case "${RUNTIME}" in
    all)
        for v in "${AGENTIC_VARIANTS[@]}"; do
            build_agentic "${v}"
        done
        build_boundary
        ;;
    claude-code|copilot-cli|codex-cli|connect-agent)
        build_agentic "${RUNTIME}"
        build_boundary
        ;;
    boundary)
        build_boundary
        ;;
    *)
        echo "ERROR: unknown runtime '${RUNTIME}'" >&2
        echo "Usage: $0 [${AGENTIC_VARIANTS[*]}|boundary|all]" >&2
        exit 1
        ;;
esac

echo
echo "OK.  Next step:"
echo "  AGENT_RUNTIME=${RUNTIME} docker compose -f docker-compose-v2.yml up --build"
