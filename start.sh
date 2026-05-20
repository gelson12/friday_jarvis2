#!/usr/bin/env bash
# Single-container launcher: runs the Python LiveKit voice worker AND
# the Next.js Friday UI side-by-side in one Railway service. If either
# process exits, the script exits non-zero so Railway's restart policy
# restarts the whole container (acceptable for a single-user deploy).
#
# Signal handling: tini (PID 1, from the Dockerfile ENTRYPOINT) forwards
# SIGTERM here; we forward to both children so Railway shutdown is clean
# instead of getting SIGKILLed after grace expires.
set -uo pipefail

WORKER_PID=""
UI_PID=""

cleanup() {
  trap - SIGTERM SIGINT EXIT
  echo "[start] cleanup — forwarding SIGTERM to children"
  [[ -n "${WORKER_PID}" ]] && kill -TERM "${WORKER_PID}" 2>/dev/null || true
  [[ -n "${UI_PID}"     ]] && kill -TERM "${UI_PID}"     2>/dev/null || true
  wait 2>/dev/null
}
trap cleanup SIGTERM SIGINT EXIT

echo "[start] launching LiveKit voice worker (agent.py)"
python /app/agent.py start &
WORKER_PID=$!

echo "[start] launching Friday UI on :${PORT:-3000}"
( cd /app/frontend && PORT="${PORT:-3000}" HOSTNAME=0.0.0.0 node server.js ) &
UI_PID=$!

# Exit the script as soon as EITHER process exits — Railway will restart
# the container. Don't try to keep one running if the other dies; the
# two are deployed as one unit and should fail as one unit.
wait -n "${WORKER_PID}" "${UI_PID}"
EXIT=$?
echo "[start] a process exited (code ${EXIT}); shutting down container"
exit "${EXIT}"
