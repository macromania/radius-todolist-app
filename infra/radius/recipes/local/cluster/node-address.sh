#!/bin/sh
set -eu

[ "$#" -eq 1 ] || {
    echo "Exactly one owned child name is required" >&2
    exit 1
}
case "$1" in
    radplanes-local-shared-control|radplanes-local-shared-data|radplanes-local-isolated-1-control|radplanes-local-isolated-1-data) ;;
    *) echo "Only reserved local child nodes are permitted" >&2; exit 1 ;;
esac
node="$1-control-plane"
label=$(docker inspect --type container --format '{{index .Config.Labels "io.x-k8s.kind.cluster"}}' "$node")
[ "$label" = "$1" ] || {
    echo "Docker node ownership label mismatch" >&2
    exit 1
}
address=$(docker inspect --type container --format '{{(index .NetworkSettings.Networks "kind").IPAddress}}' "$node")
case "$address" in
    ''|*[!0-9.]*|127.*|0.*) echo "Missing or unsafe kind network IPv4 address" >&2; exit 1 ;;
esac
printf '{"address":"%s"}\n' "$address"
