#!/bin/sh
set -eu

[ "$#" -eq 2 ] || {
    echo "A selected prefix and reserved child slot are required" >&2
    exit 1
}
# shellcheck source=image-contract.sh
. "$(dirname "$0")/image-contract.sh"
check_prefix "$1" || { echo "Invalid selected prefix" >&2; exit 1; }
case "$2" in
    shared-control|shared-data|isolated-1-control|isolated-1-data) ;;
    *) echo "Only reserved local child nodes are permitted" >&2; exit 1 ;;
esac
cluster="$1-$2"
node="$cluster-control-plane"
label=$(docker inspect --type container --format '{{index .Config.Labels "io.x-k8s.kind.cluster"}}' "$node")
[ "$label" = "$cluster" ] || {
    echo "Docker node ownership label mismatch" >&2
    exit 1
}
address=$(docker inspect --type container --format '{{(index .NetworkSettings.Networks "kind").IPAddress}}' "$node")
case "$address" in
    ''|*[!0-9.]*|127.*|0.*) echo "Missing or unsafe kind network IPv4 address" >&2; exit 1 ;;
esac
printf '{"address":"%s"}\n' "$address"
