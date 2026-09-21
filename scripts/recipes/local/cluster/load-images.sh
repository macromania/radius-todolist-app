#!/bin/sh
# Two explicit statuses preserve producer failures on shells without pipefail.
set -eu
umask 077

# shellcheck source=image-contract.sh
. "$(dirname "$0")/image-contract.sh"
check_prefix "${LOCAL_RESOURCE_PREFIX-}" || { echo "Invalid selected prefix" >&2; exit 1; }
case "${LOCAL_SLOT-}" in
    shared-control|shared-data|isolated-1-control|isolated-1-data) ;;
    *) echo "Only reserved local child nodes are permitted" >&2; exit 1 ;;
esac
[ "${LOCAL_CLUSTER-}" = "$LOCAL_RESOURCE_PREFIX-$LOCAL_SLOT" ] || {
    echo "Child name differs from the selected slot" >&2; exit 1
}
[ -n "${LOCAL_IMAGES-}" ] || { echo "No application images supplied" >&2; exit 1; }
[ -n "${LOCAL_IMAGE_IDS-}" ] || { echo "Prepared image IDs are required" >&2; exit 1; }

node="$LOCAL_CLUSTER-control-plane"
work=".radplanes-image-import-$$"
mkdir "$work"
producer=
consumer=
watchdog=
cleanup() {
    : > "$work/cancelled"
    for pid in "$producer" "$consumer" "$watchdog"; do
        [ -z "$pid" ] || kill "$pid" 2>/dev/null || :
    done
    [ -z "$watchdog" ] || wait "$watchdog" 2>/dev/null || :
    rm -f "$work/stream" "$work/images" "$work/ids" "$work/image-id" \
        "$work/label" "$work/role" "$work/loaded" "$work/cancelled"
    rmdir "$work"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

printf '%s\n' "$LOCAL_IMAGES" > "$work/images"
printf '%s\n' "$LOCAL_IMAGE_IDS" > "$work/ids"
count=0
while IFS= read -r image; do
    check_image "$LOCAL_RESOURCE_PREFIX" "$image" || {
        echo "Unapproved prepared image" >&2; exit 1
    }
    count=$((count + 1))
done < "$work/images"
if [ "$count" -lt 4 ] || [ "$count" -gt 64 ]; then
    echo "Expected the complete prepared child image set" >&2; exit 1
fi

# One deadline covers ownership checks, every import, and ctr verification.
parent=$$
(
    child_timer=
    cancelled=no
    trap '[ -z "$child_timer" ] || { kill "$child_timer" 2>/dev/null || :; wait "$child_timer" 2>/dev/null || :; }' EXIT
    # Defer cancellation until the timer PID is assigned; the marker also covers
    # cancellation before this subshell installs its signal handlers.
    trap 'cancelled=yes' HUP INT TERM
    sleep 600 &
    child_timer=$!
    trap 'exit 0' HUP INT TERM
    [ "$cancelled" = no ] && [ ! -f "$work/cancelled" ] || exit 0
    wait "$child_timer"
    child_timer=
    echo "Image distribution exceeded 600s; remote Docker execution may continue" >&2
    kill -TERM "$parent"
) &
watchdog=$!

docker inspect --type container --format '{{index .Config.Labels "io.x-k8s.kind.cluster"}}' "$node" > "$work/label" &
consumer=$!
wait "$consumer"
consumer=
read -r label < "$work/label"
docker inspect --type container --format '{{index .Config.Labels "io.x-k8s.kind.role"}}' "$node" > "$work/role" &
consumer=$!
wait "$consumer"
consumer=
read -r role < "$work/role"
if [ "$label" != "$LOCAL_CLUSTER" ] || [ "$role" != control-plane ]; then
    echo "Docker child ownership labels mismatch" >&2; exit 1
fi
mkfifo "$work/stream"
exec 3< "$work/ids"
while IFS= read -r image; do
    IFS= read -r expected <&3 || { echo "Missing prepared image ID" >&2; exit 1; }
    docker image inspect --format '{{.Id}}' "$image" > "$work/image-id" &
    consumer=$!
    wait "$consumer"
    consumer=
    IFS= read -r actual < "$work/image-id"
    [ "$actual" = "$expected" ] || { echo "Prepared image changed before import" >&2; exit 1; }
    docker image save "$image" > "$work/stream" &
    producer=$!
    docker exec -i "$node" ctr --namespace k8s.io images import - < "$work/stream" &
    consumer=$!
    saved=0
    imported=0
    wait "$producer" || saved=$?
    producer=
    wait "$consumer" || imported=$?
    consumer=
    if [ "$saved" -ne 0 ] || [ "$imported" -ne 0 ]; then
        echo "Image stream failed: save=$saved import=$imported" >&2; exit 1
    fi
    docker exec "$node" ctr --namespace k8s.io images list --quiet > "$work/loaded" &
    consumer=$!
    wait "$consumer"
    consumer=
    grep -F -x -- "$image" "$work/loaded" >/dev/null || {
        echo "Imported image reference is absent from the child" >&2; exit 1
    }
done < "$work/images"
if IFS= read -r _unexpected <&3; then
    echo "Unexpected extra image IDs" >&2
    exit 1
fi
exec 3<&-
