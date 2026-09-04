#!/usr/bin/env bash
# Prove that todo data really left the cluster and landed in Azure Managed Redis.
#
# This is the strongest acceptance test in the project, and it checks three
# things that each fail quietly:
#
#   1. The application is using Redis at all, rather than silently falling back
#      to storing todos in process memory.
#   2. TLS is actually on. If the Recipe forgot `tls: true`, Radius builds a
#      plaintext redis:// URL and every request returns 500.
#   3. The data is in Azure, read back out of the managed service directly.
#
# The Redis read runs from a pod inside the cluster, not from this laptop,
# because the cache has no public endpoint. That is also the better test: it
# exercises the private endpoint the application actually uses.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source ports.env

AKS_CONTEXT="${AKS_CONTEXT:-aks-todolist}"
NS="${AZURE_NS:-todolist-azure-todolist}"
APP_RG="${APP_RG:-rg-todolist-app}"
TITLE="from azure managed redis ${RANDOM}"

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "==> 1/4 confirming the application chose Redis, not in-memory"
STORE=$(kubectl --context "$AKS_CONTEXT" -n "$NS" logs deploy/demo --tail=200 | grep -i 'using ' | tail -1 || true)
echo "    $STORE"
case "$STORE" in
  *"found url in environment variable CONNECTION_REDIS_URL"*) ;;
  *in-memory*) fail "the application fell back to in-memory storage";;
  *) fail "could not determine the datastore from the logs";;
esac

echo "==> 2/4 confirming TLS is on"
TLS=$(kubectl --context "$AKS_CONTEXT" -n "$NS" \
  get secret demo -o jsonpath='{.data.CONNECTION_REDIS_TLS}' | base64 -d)
echo "    CONNECTION_REDIS_TLS=$TLS"
[ "$TLS" = "true" ] || fail "TLS is off; the Recipe is not emitting tls: true"

HOST=$(kubectl --context "$AKS_CONTEXT" -n "$NS" \
  get secret demo -o jsonpath='{.data.CONNECTION_REDIS_HOST}' | base64 -d)
echo "    host=$HOST"
case "$HOST" in
  *.redis.azure.net) ;;
  *) fail "host is not an Azure Managed Redis endpoint: $HOST";;
esac

echo "==> 3/4 creating a todo through the application: ${TITLE}"
kubectl --context "$AKS_CONTEXT" -n "$NS" port-forward deploy/demo \
  "${SCRATCH_PORT}:${APP_PORT}" >/dev/null 2>&1 &
PF=$!
trap '{ kill "$PF" && wait "$PF"; } 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do
  curl -sf -o /dev/null "http://localhost:${SCRATCH_PORT}/healthz" && break
  sleep 1
done
curl -sf -o /dev/null "http://localhost:${SCRATCH_PORT}/healthz" \
  || fail "/healthz did not return 200; Redis is unreachable from the pod"
curl -sf -X POST "http://localhost:${SCRATCH_PORT}/api/todos" \
  -H 'Content-Type: application/json' \
  -d "{\"title\":\"${TITLE}\",\"done\":false}" >/dev/null
{ kill "$PF" && wait "$PF"; } 2>/dev/null || true; trap - EXIT

echo "==> 4/4 reading it back out of Azure Managed Redis from inside the cluster"
CACHE=$(az redisenterprise list -g "$APP_RG" --query "[0].name" -o tsv)
[ -n "$CACHE" ] || fail "no Azure Managed Redis instance found in $APP_RG"
# redis-cli needs the raw key. The one in the Kubernetes Secret is
# percent-encoded, because Radius embeds it in a URL without encoding it and
# base64 keys contain '/' about half the time.
KEY=$(az redisenterprise database list-keys --cluster-name "$CACHE" -g "$APP_RG" \
  --query primaryKey -o tsv)
[ -n "$KEY" ] || fail "could not read the access key"

# Todos live in a single Redis hash named 'items'. GET returns WRONGTYPE.
# REDISCLI_AUTH keeps the key out of the process argument list.
OUT=$(kubectl --context "$AKS_CONTEXT" -n "$NS" run redis-probe-$$ \
  --rm -i --restart=Never --quiet \
  --image=redis:7-alpine \
  --env="REDISCLI_AUTH=${KEY}" \
  --command -- redis-cli -h "$HOST" -p 10000 --tls HVALS items 2>/dev/null)

if grep -qF "$TITLE" <<<"$OUT"; then
  echo
  echo "PASS: the todo was read back out of Azure Managed Redis."
  echo "      cache: $CACHE"
  echo "      host:  $HOST (private endpoint, no public network access)"
else
  echo "$OUT" >&2
  fail "the todo was not found in Azure Managed Redis"
fi
