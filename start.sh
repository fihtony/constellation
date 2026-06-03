#!/usr/bin/env bash
# start.sh — Launch the Constellation v2 stack.
#
# Usage:  ./start.sh [docker|rancher] [build] [run]
#
# Defaults: runtime = docker, action = run (no build).
#
# Examples:
#   ./start.sh                        # docker + run            (default)
#   ./start.sh docker                 # docker + run
#   ./start.sh rancher                # rancher + run
#   ./start.sh rancher build          # rancher + build only
#   ./start.sh rancher build run      # rancher + build then run
#   ./start.sh docker build run       # docker + build then run
#
# The "build" step rebuilds every constellation image:
#   * Long-running services declared in the chosen compose file
#     (registry, init-register, compass, team-lead, jira, scm, ui-design)
#     via `docker compose build`.
#   * Per-task agents (office, web-dev, code-review) via explicit
#     `docker build`, since they are NOT declared in the compose files —
#     they are launched dynamically by compass / team-lead through the
#     docker socket. All images are tagged ":latest".

set -euo pipefail

usage() {
    sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
}

# -------------------------------------------------------------------- argv ---
runtime=""
do_build=0
do_run=0

for arg in "$@"; do
    case "$arg" in
        docker|rancher)
            if [ -n "$runtime" ]; then
                echo "Error: runtime specified twice ('$runtime' then '$arg')." >&2
                exit 2
            fi
            runtime="$arg"
            ;;
        build) do_build=1 ;;
        run)   do_run=1   ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "Error: unknown argument '$arg'." >&2
            usage >&2
            exit 2
            ;;
    esac
done

# Defaults: docker runtime, "run" action when neither build nor run was given.
[ -z "$runtime" ] && runtime="docker"
if [ "$do_build" -eq 0 ] && [ "$do_run" -eq 0 ]; then
    do_run=1
fi

# --------------------------------------------------------------- runtime ---
case "$runtime" in
    docker)  compose_file="docker-compose-v2.yml"         ;;
    rancher) compose_file="docker-compose-v2.rancher.yml" ;;
esac

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

if [ ! -f "$compose_file" ]; then
    echo "Error: compose file '$compose_file' not found in $script_dir" >&2
    exit 1
fi

# Per-task agents that are launched dynamically and therefore NOT part of
# the compose file. Format: "<image-suffix>|<dockerfile-dir>".
per_task_agents=(
    "office|office"
    "web-dev|web_dev"
    "code-review|code_review"
)

# ---------------------------------------------------------------- build ---
if [ "$do_build" -eq 1 ]; then
    echo "==> [build] long-running services via $compose_file"
    docker compose -f "$compose_file" build

    echo "==> [build] per-task agent images (tagged :latest)"
    for spec in "${per_task_agents[@]}"; do
        image_suffix="${spec%%|*}"
        dir_name="${spec##*|}"
        tag="constellation-v2-${image_suffix}:latest"
        echo "    - $tag  (agents/${dir_name}/Dockerfile)"
        docker build --tag "$tag" \
                     --file "agents/${dir_name}/Dockerfile" \
                     .
    done
fi

# ------------------------------------------------------------------ run ---
if [ "$do_run" -eq 1 ]; then
    echo "==> [run] starting Constellation ($runtime → $compose_file)"
    docker compose -f "$compose_file" up -d
    echo
    docker compose -f "$compose_file" ps
fi
