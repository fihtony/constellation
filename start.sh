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
# The "build" step runs in this order:
#   1. The shared base image(s), via scripts/build_base.sh.  The
#      base carries the system packages, the union of every
#      agent's Python deps, and the agentic CLI (when the
#      deployment uses one).  The base is tagged
#        constellation-base:agentic-<AGENT_RUNTIME>   (full)
#        constellation-base:boundary                 (slim; boundary agents)
#      and is the FROM source for every per-agent Dockerfile
#      in agents/<name>/.
#   2. Long-running services declared in the chosen compose file
#      (registry, init-register, compass, team-lead, jira, scm,
#      ui-design).  These build on top of the base; the
#      per-agent Dockerfiles pass through the AGENT_RUNTIME
#      build arg from config/.env.
#   3. Per-task agents (office, web-dev, code-review).  These are
#      NOT declared in the compose file — they are launched
#      dynamically by compass / team-lead through the docker
#      socket.  We build them explicitly here, tagged ":latest"
#      with the AGENT_RUNTIME build arg.
#
# The "run" step does `docker compose -f <files> up -d` followed
# by `docker compose -f <files> ps`.

set -euo pipefail

usage() {
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
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

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

# --------------------------------------------------------------- runtime ---
# The two compose files.  For docker we use the main file alone;
# for rancher we merge the main file with the minimal override
# (which only changes DOCKER_SOCKET_GID for the services that
# mount the host docker socket).  See docker-compose.v2.rancher.yml
# for the override contract.
# Each ``-f`` flag and its filename are stored as separate
# array elements so that ``"${compose_args[@]}"`` at the
# ``docker compose`` call site below produces the right argv
# (``docker compose -f main.yml -f override.yml build``).
case "$runtime" in
    docker)
        compose_args=(-f docker-compose-v2.yml)
        ;;
    rancher)
        compose_args=(-f docker-compose-v2.yml -f docker-compose.v2.rancher.yml)
        ;;
esac

# Validate the compose file(s) before doing any work.  Walk the
# argv-style array in pairs: an ``-f`` element followed by the
# filename element.
for ((i = 0; i < ${#compose_args[@]}; i++)); do
    if [ "${compose_args[$i]}" = "-f" ]; then
        filename="${compose_args[$((i + 1))]}"
        if [ ! -f "$filename" ]; then
            echo "Error: compose file '$filename' not found in $script_dir" >&2
            exit 1
        fi
        i=$((i + 1))
    fi
done

# ----------------------------------------------------------- agent_runtime ---
# Pick the AGENT_RUNTIME that drives both the base image build
# (scripts/build_base.sh) and the per-agent compose build args
# (docker-compose-v2.yml forwards it).  Precedence:
#   1. $AGENT_RUNTIME in the calling shell
#   2. AGENT_RUNTIME=... in config/.env
#   3. claude-code (the default in pyproject / framework/config.py)
if [ -z "${AGENT_RUNTIME:-}" ] && [ -f config/.env ]; then
    AGENT_RUNTIME="$(grep -E '^AGENT_RUNTIME=' config/.env | head -1 | cut -d= -f2- | tr -d '"' || true)"
fi
: "${AGENT_RUNTIME:=claude-code}"
export AGENT_RUNTIME
echo "==> AGENT_RUNTIME=${AGENT_RUNTIME}"

# Per-task agents that are launched dynamically and therefore NOT part of
# the compose file.  Format: "<image-suffix>|<dockerfile-dir>".
per_task_agents=(
    "office|office"
    "web-dev|web_dev"
    "code-review|code_review"
)

# ---------------------------------------------------------------- build ---
if [ "$do_build" -eq 1 ]; then
    echo "==> [build] base image(s) via scripts/build_base.sh ${AGENT_RUNTIME}"
    ./scripts/build_base.sh "${AGENT_RUNTIME}"

    echo "==> [build] long-running services via ${compose_args[*]}"
    docker compose "${compose_args[@]}" build

    echo "==> [build] per-task agent images (tagged :latest)"
    for spec in "${per_task_agents[@]}"; do
        image_suffix="${spec%%|*}"
        dir_name="${spec##*|}"
        tag="constellation-v2-${image_suffix}:latest"
        echo "    - $tag  (agents/${dir_name}/Dockerfile, AGENT_RUNTIME=${AGENT_RUNTIME})"
        docker build --tag "$tag" \
                     --file "agents/${dir_name}/Dockerfile" \
                     --build-arg "AGENT_RUNTIME=${AGENT_RUNTIME}" \
                     .
    done
fi

# ------------------------------------------------------------------ run ---
if [ "$do_run" -eq 1 ]; then
    echo "==> [run] starting Constellation ($runtime → ${compose_args[*]})"
    docker compose "${compose_args[@]}" up -d
    echo
    docker compose "${compose_args[@]}" ps
fi
