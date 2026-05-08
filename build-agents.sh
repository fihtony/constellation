#!/usr/bin/env bash
# build-agents.sh — Build Docker images for Constellation agents.
#
# Dynamic agents (team-lead, web, android, office) are NOT started by docker-compose;
# they are launched at runtime by the Compass agent via the Docker API when a matching
# task arrives. Their images must be built manually before starting the compose stack.
#
# Persistent agents (compass, jira, scm, ui-design) are started by docker-compose
# and should be rebuilt when their source or backend changes.
#
# This script first builds the shared base images (constellation-base:latest and
# the backend-specific extension bases), then builds each requested agent image.
#
# Usage:
#   ./build-agents.sh              # build base images + all dynamic agent images
#   ./build-agents.sh web          # build base images + web agent only
#   ./build-agents.sh android      # build base images + android agent only
#   ./build-agents.sh team-lead    # build base images + team-lead agent only
#   ./build-agents.sh office       # build base images + office agent only
#   ./build-agents.sh compass      # build base images + compass agent only
#   ./build-agents.sh jira         # build base images + jira agent only
#   ./build-agents.sh scm          # build base images + scm agent only
#   ./build-agents.sh ui-design    # build base images + ui-design agent only
#   ./build-agents.sh base         # build base images only
#   ./build-agents.sh all          # build base images + all agent images
#
# After building, start the compose stack as usual:
#   docker compose up --build -d
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Resolve effective backend for an agent by parsing .env files
resolve_backend() {
    local agent="$1"
    python3 "${SCRIPT_DIR}/scripts/resolve-agent-runtime.py" "$agent" 2>/dev/null | grep '^backend=' | cut -d= -f2 || echo "copilot-cli"
}

# ── Base image builders ───────────────────────────────────────────────────────

build_base_images() {
    echo "==> Building constellation base image: constellation-base:latest"
    docker build \
        -t constellation-base:latest \
        -f "${SCRIPT_DIR}/dockerfiles/base.Dockerfile" \
        "${SCRIPT_DIR}"
    echo "    Done: constellation-base:latest"

    echo "==> Building constellation-base-copilot-cli:latest"
    docker build \
        -t constellation-base-copilot-cli:latest \
        -f "${SCRIPT_DIR}/dockerfiles/base-copilot-cli.Dockerfile" \
        "${SCRIPT_DIR}"
    echo "    Done: constellation-base-copilot-cli:latest"

    echo "==> Building constellation-base-claude-code:latest"
    docker build \
        -t constellation-base-claude-code:latest \
        -f "${SCRIPT_DIR}/dockerfiles/base-claude-code.Dockerfile" \
        "${SCRIPT_DIR}"
    echo "    Done: constellation-base-claude-code:latest"

    echo "==> Building constellation-base-connect-agent:latest"
    docker build \
        -t constellation-base-connect-agent:latest \
        -f "${SCRIPT_DIR}/dockerfiles/base-connect-agent.Dockerfile" \
        "${SCRIPT_DIR}"
    echo "    Done: constellation-base-connect-agent:latest"
}

# ── Agent builders ────────────────────────────────────────────────────────────

build_android() {
    if [[ ! -d "${SCRIPT_DIR}/android" ]]; then
        echo "android agent source not present in this repository"
        return 1
    fi
    local backend
    backend=$(resolve_backend android)
    local dockerfile="${SCRIPT_DIR}/android/Dockerfile.${backend}"
    if [[ ! -f "$dockerfile" ]]; then
        echo "ERROR: Missing Dockerfile for backend '${backend}': ${dockerfile}"
        echo "Each agent requires a runtime-specific Dockerfile (e.g. Dockerfile.connect-agent)."
        return 1
    fi
    echo "==> Building android agent image: constellation-android-agent:latest (backend: ${backend})"
    docker build \
        --platform linux/amd64 \
        -t constellation-android-agent:latest \
        -f "$dockerfile" \
        "${SCRIPT_DIR}"
    echo "    Done: constellation-android-agent:latest"
}

build_web() {
    local backend
    backend=$(resolve_backend web)
    local dockerfile="${SCRIPT_DIR}/web/Dockerfile.${backend}"
    if [[ ! -f "$dockerfile" ]]; then
        echo "ERROR: Missing Dockerfile for backend '${backend}': ${dockerfile}"; echo "Each agent requires a runtime-specific Dockerfile (e.g. Dockerfile.connect-agent)."; return 1
    fi
    echo "==> Building web agent image: constellation-web-agent:latest (backend: ${backend})"
    docker build \
        -t constellation-web-agent:latest \
        -f "$dockerfile" \
        "${SCRIPT_DIR}"
    echo "    Done: constellation-web-agent:latest"
}

build_team_lead() {
    local backend
    backend=$(resolve_backend team-lead)
    local dockerfile="${SCRIPT_DIR}/team-lead/Dockerfile.${backend}"
    if [[ ! -f "$dockerfile" ]]; then
        echo "ERROR: Missing Dockerfile for backend '${backend}': ${dockerfile}"; echo "Each agent requires a runtime-specific Dockerfile (e.g. Dockerfile.connect-agent)."; return 1
    fi
    echo "==> Building team-lead agent image: constellation-team-lead-agent:latest (backend: ${backend})"
    docker build \
        -t constellation-team-lead-agent:latest \
        -f "$dockerfile" \
        "${SCRIPT_DIR}"
    echo "    Done: constellation-team-lead-agent:latest"
}

build_office() {
    if [[ ! -d "${SCRIPT_DIR}/office" ]]; then
        echo "office agent source not present in this repository"
        return 1
    fi
    local backend
    backend=$(resolve_backend office)
    local dockerfile="${SCRIPT_DIR}/office/Dockerfile.${backend}"
    if [[ ! -f "$dockerfile" ]]; then
        echo "ERROR: Missing Dockerfile for backend '${backend}': ${dockerfile}"; echo "Each agent requires a runtime-specific Dockerfile (e.g. Dockerfile.connect-agent)."; return 1
    fi
    echo "==> Building office agent image: constellation-office-agent:latest (backend: ${backend})"
    docker build \
        -t constellation-office-agent:latest \
        -f "$dockerfile" \
        "${SCRIPT_DIR}"
    echo "    Done: constellation-office-agent:latest"
}

build_compass() {
    local backend
    backend=$(resolve_backend compass)
    local dockerfile="${SCRIPT_DIR}/compass/Dockerfile.${backend}"
    if [[ ! -f "$dockerfile" ]]; then
        echo "ERROR: Missing Dockerfile for backend '${backend}': ${dockerfile}"; echo "Each agent requires a runtime-specific Dockerfile (e.g. Dockerfile.connect-agent)."; return 1
    fi
    echo "==> Building compass agent image: constellation-compass:latest (backend: ${backend})"
    docker build \
        -t constellation-compass:latest \
        -f "$dockerfile" \
        "${SCRIPT_DIR}"
    echo "    Done: constellation-compass:latest"
}

build_jira() {
    local backend
    backend=$(resolve_backend jira)
    local dockerfile="${SCRIPT_DIR}/jira/Dockerfile.${backend}"
    if [[ ! -f "$dockerfile" ]]; then
        echo "ERROR: Missing Dockerfile for backend '${backend}': ${dockerfile}"; echo "Each agent requires a runtime-specific Dockerfile (e.g. Dockerfile.connect-agent)."; return 1
    fi
    echo "==> Building jira agent image: constellation-jira-agent:latest (backend: ${backend})"
    docker build \
        -t constellation-jira-agent:latest \
        -f "$dockerfile" \
        "${SCRIPT_DIR}"
    echo "    Done: constellation-jira-agent:latest"
}

build_scm() {
    local backend
    backend=$(resolve_backend scm)
    local dockerfile="${SCRIPT_DIR}/scm/Dockerfile.${backend}"
    if [[ ! -f "$dockerfile" ]]; then
        echo "ERROR: Missing Dockerfile for backend '${backend}': ${dockerfile}"; echo "Each agent requires a runtime-specific Dockerfile (e.g. Dockerfile.connect-agent)."; return 1
    fi
    echo "==> Building scm agent image: constellation-scm-agent:latest (backend: ${backend})"
    docker build \
        -t constellation-scm-agent:latest \
        -f "$dockerfile" \
        "${SCRIPT_DIR}"
    echo "    Done: constellation-scm-agent:latest"
}

build_ui_design() {
    local backend
    backend=$(resolve_backend ui-design)
    local dockerfile="${SCRIPT_DIR}/ui-design/Dockerfile.${backend}"
    if [[ ! -f "$dockerfile" ]]; then
        echo "ERROR: Missing Dockerfile for backend '${backend}': ${dockerfile}"; echo "Each agent requires a runtime-specific Dockerfile (e.g. Dockerfile.connect-agent)."; return 1
    fi
    echo "==> Building ui-design agent image: constellation-ui-design-agent:latest (backend: ${backend})"
    docker build \
        -t constellation-ui-design-agent:latest \
        -f "$dockerfile" \
        "${SCRIPT_DIR}"
    echo "    Done: constellation-ui-design-agent:latest"
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
TARGET="${1:-all}"

case "$TARGET" in
    base)
        build_base_images
        ;;
    android)
        build_base_images
        build_android
        ;;
    web)
        build_base_images
        build_web
        ;;
    team-lead)
        build_base_images
        build_team_lead
        ;;
    office)
        build_base_images
        build_office
        ;;
    compass)
        build_base_images
        build_compass
        ;;
    jira)
        build_base_images
        build_jira
        ;;
    scm)
        build_base_images
        build_scm
        ;;
    ui-design)
        build_base_images
        build_ui_design
        ;;
    all)
        build_base_images
        build_web
        build_team_lead
        [[ -d "${SCRIPT_DIR}/office" ]] && build_office || echo "==> Skipping office agent (not found)"
        [[ -d "${SCRIPT_DIR}/android" ]] && build_android || echo "==> Skipping android agent (not found)"
        ;;
    *)
        echo "Unknown target: $TARGET"
        echo "Usage: $0 [base|android|web|team-lead|office|compass|jira|scm|ui-design|all]"
        exit 1
        ;;
esac

echo ""
echo "All requested images built successfully."
