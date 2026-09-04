#!/usr/bin/env bash
# Prove a todo survives a pod restart on the kind cluster.
#
# Before Redis was wired up this script fails: the final list comes back empty
# because the replacement pod has a fresh in-memory store.
#
# Note the port-forward is established twice. `kubectl port-forward` attaches to
# one specific pod and dies with it, so reusing the first port-forward across
# the delete makes this test fail even when persistence works correctly.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source ports.env

CONTEXT="${KIND_CONTEXT:-kind-radius-todolist-app}"
NS="${LOCAL_NS:-todolist-local-todolist}"
TITLE="survives a restart ${RANDOM}"

forward() {
  kubectl --context "$CONTEXT" -n "$NS" port-forward deploy/demo \
    "${SCRATCH_PORT}:${APP_PORT}" >/dev/null 2>&1 &
  echo $!
}

wait_ready() {
  for _ in $(seq 1 30); do
    if curl -sf -o /dev/null "http://localhost:${SCRATCH_PORT}/healthz"; then return 0; fi
    sleep 1
  done
  echo "FAIL: /healthz never returned 200; Redis is probably unreachable" >&2
  return 1
}

echo "==> creating a todo: ${TITLE}"
PF=$(forward); trap '{ kill "$PF" && wait "$PF"; } 2>/dev/null || true' EXIT
wait_ready
curl -sf -X POST "http://localhost:${SCRATCH_PORT}/api/todos" \
  -H 'Content-Type: application/json' \
  -d "{\"title\":\"${TITLE}\",\"done\":false}" >/dev/null
{ kill "$PF" && wait "$PF"; } 2>/dev/null || true; trap - EXIT

echo "==> deleting the pod"
kubectl --context "$CONTEXT" -n "$NS" delete pod -l radapp.io/resource=demo --wait=true
kubectl --context "$CONTEXT" -n "$NS" rollout status deploy/demo --timeout=120s

echo "==> reading the todo back from the replacement pod"
PF=$(forward); trap '{ kill "$PF" && wait "$PF"; } 2>/dev/null || true' EXIT
wait_ready
BODY=$(curl -sf "http://localhost:${SCRATCH_PORT}/api/todos")
{ kill "$PF" && wait "$PF"; } 2>/dev/null || true; trap - EXIT

if grep -qF "$TITLE" <<<"$BODY"; then
  echo "PASS: the todo survived the restart"
else
  echo "FAIL: the todo did not survive. Response was:" >&2
  echo "$BODY" >&2
  exit 1
fi
