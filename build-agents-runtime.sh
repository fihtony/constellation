#!/usr/bin/env bash
# build-agents-runtime.sh — Runtime-aware Docker image builder.
#
# Selects the correct Dockerfile.<backend> for each agent based on AGENT_RUNTIME.
# Only rebuilds agents whose runtime fingerprint has changed since the last build.
#
# Usage:
#   ./build-agents-runtime.sh              # build all agents using current AGENT_RUNTIME
#   ./build-agents-runtime.sh team-lead    # build only the team-lead agent
#   ./build-agents-runtime.sh --backend connect-agent web  # explicit backend override
#   ./build-agents-runtime.sh --force all  # force rebuild even if fingerprint unchanged
#
# Supported backends: copilot-cli (default), connect-agent, claude-code
#
# How it works:
#   1. Reads AGENT_RUNTIME from common/.env (or environment override).
#   2. Checks if a Dockerfile.<backend> exists for the agent; falls back to Dockerfile.
#   3. Computes a runtime fingerprint: sha256 of (AGENT_RUNTIME + Dockerfile contents).
#   4. Skips the build if the fingerprint matches the label on the last built image.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# ── Parse arguments ────────────────────────────────────────────────────────────
FORCE=0
BACKEND_OVERRIDE=""
TARGETS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force|-f)
            FORCE=1
            shift
            ;;
        --backend|-b)
            BACKEND_OVERRIDE="$2"
            shift 2
            ;;
        *)
            TARGETS+=("$1")
            shift
            ;;
    esac
done

[[ ${#TARGETS[@]} -eq 0 ]] && TARGETS=("all")

# ── Resolve effective backend ──────────────────────────────────────────────────
# Priority: command-line --backend > AGENT_RUNTIME env var > common/.env > default
resolve_backend() {
    if [[ -n "$BACKEND_OVERRIDE" ]]; then
        echo "$BACKEND_OVERRIDE"
        return
    fi
    if [[ -n "${AGENT_RUNTIME:-}" ]]; then
        echo "$AGENT_RUNTIME"
        return
    fi
    # Try to read from common/.env
    local common_env="${SCRIPT_DIR}/common/.env"
    if [[ -f "$common_env" ]]; then
        local val
        val=$(grep -E '^AGENT_RUNTIME=' "$common_env" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
        if [[ -n "$val" ]]; then
            echo "$val"
            return
        fi
    fi
    # Default
    echo "copilot-cli"
}

BACKEND=$(resolve_backend)
echo "==> Effective backend: $BACKEND"

# ── Validate backend ───────────────────────────────────────────────────────────
case "$BACKEND" in
    copilot-cli|connect-agent|claude-code)
        ;;
    *)
        echo "ERROR: Unknown backend '$BACKEND'. Supported: copilot-cli, connect-agent, claude-code" >&2
        exit 1
        ;;
esac

# ── Fingerprint helpers ────────────────────────────────────────────────────────
# Computes a short fingerprint for the runtime build context.
# Includes: backend name + Dockerfile contents.
compute_fingerprint() {
    local dockerfile="$1"
    echo "${BACKEND}:$(sha256sum "$dockerfile" | cut -c1-16)"
}

# Reads the stored fingerprint label from the last built image.
get_image_fingerprint() {
    local image="$1"
    docker inspect --format '{{ index .Config.Labels "constellation.runtime.fingerprint" }}' \
        "$image" 2>/dev/null || echo ""
}

# ── Build one agent ────────────────────────────────────────────────────────────
build_agent() {
    local agent_name="$1"   # e.g. "team-lead"
    local image_name="$2"   # e.g. "constellation-team-lead-agent:latest"
    local -a extra_args=()
    if [[ $# -gt 2 ]]; then
        extra_args=("${@:3}")
    fi

    local agent_dir="${SCRIPT_DIR}/${agent_name}"
    if [[ ! -d "$agent_dir" ]]; then
        echo "WARNING: Agent directory not found: $agent_dir — skipping." >&2
        return 0
    fi

    # Resolve Dockerfile: prefer Dockerfile.<backend>, fall back to Dockerfile.
    local dockerfile="${agent_dir}/Dockerfile.${BACKEND}"
    if [[ ! -f "$dockerfile" ]]; then
        echo "    No Dockerfile.${BACKEND} found for ${agent_name}; falling back to Dockerfile."
        dockerfile="${agent_dir}/Dockerfile"
    fi
    if [[ ! -f "$dockerfile" ]]; then
        echo "WARNING: No Dockerfile found for ${agent_name} — skipping." >&2
        return 0
    fi

    local fingerprint
    fingerprint=$(compute_fingerprint "$dockerfile")

    # Check if rebuild is needed.
    if [[ "$FORCE" -eq 0 ]]; then
        local existing_fp
        existing_fp=$(get_image_fingerprint "$image_name")
        if [[ "$existing_fp" == "$fingerprint" ]]; then
            echo "    ${image_name} is up-to-date (fingerprint: ${fingerprint}) — skipping."
            return 0
        fi
    fi

    echo "==> Building ${image_name} [backend=${BACKEND}, dockerfile=$(basename "$dockerfile")]"
    local -a build_cmd=(docker build)
    if [[ ${#extra_args[@]} -gt 0 ]]; then
        build_cmd+=("${extra_args[@]}")
    fi
    build_cmd+=(
        --label "constellation.runtime.fingerprint=${fingerprint}"
        --label "constellation.runtime.backend=${BACKEND}"
        -t "$image_name"
        -f "$dockerfile"
        "${SCRIPT_DIR}"
    )
    "${build_cmd[@]}"
    echo "    Done: ${image_name}"
}

# ── Per-agent build functions ──────────────────────────────────────────────────
build_team_lead() {
    build_agent "team-lead" "constellation-team-lead-agent:latest"
}

build_web() {
    build_agent "web" "constellation-web-agent:latest"
}

build_android() {
    build_agent "android" "constellation-android-agent:latest" \
        --platform linux/amd64
}

build_office() {
    build_agent "office" "constellation-office-agent:latest"
}

build_compass() {
    build_agent "compass" "constellation-compass-agent:latest"
}

build_jira() {
    build_agent "jira" "constellation-jira-agent:latest"
}

build_scm() {
    build_agent "scm" "constellation-scm-agent:latest"
}

build_ui_design() {
    build_agent "ui-design" "constellation-ui-design-agent:latest"
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
for TARGET in "${TARGETS[@]}"; do
    case "$TARGET" in
        android)       build_android ;;
        web)           build_web ;;
        team-lead)     build_team_lead ;;
        office)        build_office ;;
        compass)       build_compass ;;
        jira)          build_jira ;;
        scm)           build_scm ;;
        ui-design)     build_ui_design ;;
        all)
            build_team_lead
            build_web
            build_office
            build_android
            build_compass
            build_jira
            build_scm
            build_ui_design
            ;;
        *)
            echo "ERROR: Unknown target '$TARGET'." >&2
            echo "Usage: $0 [--backend <backend>] [--force] [team-lead|web|android|office|compass|jira|scm|ui-design|all]" >&2
            exit 1
            ;;
    esac
done
