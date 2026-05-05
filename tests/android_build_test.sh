#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DOCKER_HOST:-}" && -S "${HOME}/.rd/docker.sock" ]]; then
	export DOCKER_HOST="unix://${HOME}/.rd/docker.sock"
fi

REPO="${REPO:-${HOME}/study/ai2/constellation/reference/android-test}"
IMAGE="${IMAGE:-constellation-android-agent:latest}"
GRADLE_USER_HOME_REL="${GRADLE_USER_HOME_REL:-.gradle-agent}"
MEMORY_LIMIT="${MEMORY_LIMIT:-4g}"
HEARTBEAT_SECONDS="${HEARTBEAT_SECONDS:-20}"
QUIET_TAIL_LINES="${QUIET_TAIL_LINES:-12}"
LOG_FILE="${LOG_FILE:-/tmp/android_build_test.$(date +%Y%m%d-%H%M%S).log}"

targets=("$@")
if [[ ${#targets[@]} -eq 0 ]]; then
	targets=(testDebugUnitTest)
fi

if [[ ! -d "$REPO" ]]; then
	echo "Repository not found: $REPO" >&2
	exit 1
fi

mkdir -p "$REPO/$GRADLE_USER_HOME_REL"

printf -v quoted_targets '%q ' "${targets[@]}"
container_name="android-build-test-$(date +%s)-$$"
status_file="$(mktemp /tmp/android-build-status.XXXXXX)"

cleanup() {
	docker rm -f "$container_name" >/dev/null 2>&1 || true
	rm -f "$status_file"
}
trap cleanup EXIT INT TERM

runner_script=$(cat <<EOF
set -e
rm -f "\$GRADLE_USER_HOME/caches/journal-1/journal-1.lock"
./gradlew ${quoted_targets} --no-daemon --console=plain --max-workers=1 \
	-Pkotlin.compiler.execution.strategy=in-process \
	-Dkotlin.daemon.enabled=false \
	-Dorg.gradle.vfs.watch=false
EOF
)

echo "Android test monitor"
echo "  repo:        $REPO"
echo "  image:       $IMAGE"
echo "  targets:     ${targets[*]}"
echo "  docker host: ${DOCKER_HOST:-default}"
echo "  memory:      $MEMORY_LIMIT"
echo "  log file:    $LOG_FILE"
echo ""

(
	set -o pipefail
	docker run --name "$container_name" --rm --platform linux/amd64 --memory="$MEMORY_LIMIT" \
		-v "$REPO:/workspace" \
		-w /workspace \
		-e GRADLE_USER_HOME="/workspace/$GRADLE_USER_HOME_REL" \
		"$IMAGE" \
		sh -lc "$runner_script" 2>&1 | tee "$LOG_FILE"
	printf '%s\n' "${PIPESTATUS[0]}" > "$status_file"
) &
runner_pid=$!

last_size=-1
while kill -0 "$runner_pid" 2>/dev/null; do
	sleep "$HEARTBEAT_SECONDS"
	if ! kill -0 "$runner_pid" 2>/dev/null; then
		break
	fi

	current_size=0
	if [[ -f "$LOG_FILE" ]]; then
		current_size=$(wc -c < "$LOG_FILE")
		current_size=${current_size//[[:space:]]/}
	fi

	if [[ "$current_size" == "$last_size" ]]; then
		container_state=$(docker inspect -f '{{.State.Status}}' "$container_name" 2>/dev/null || printf 'unavailable')
		echo ""
		echo "[$(date '+%Y-%m-%d %H:%M:%S')] heartbeat: no new log output for ${HEARTBEAT_SECONDS}s; container=$container_state"
		if [[ -f "$LOG_FILE" ]]; then
			echo "Recent output:"
			tail -n "$QUIET_TAIL_LINES" "$LOG_FILE"
		fi
		echo ""
	fi

	last_size="$current_size"
done

wait "$runner_pid" || true
exit_code=1
if [[ -f "$status_file" ]]; then
	exit_code=$(cat "$status_file")
fi

echo ""
echo "Completed with exit code: $exit_code"
echo "Log saved to: $LOG_FILE"
exit "$exit_code"
