#!/bin/sh
set -eu

[ "$#" -eq 1 ] && [ "$1" = radplanes-local-shared-control ] || {
    echo "Only the bounded shared-control gate node is permitted" >&2
    exit 1
}
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
