#!/bin/sh
# Two explicit statuses preserve producer failures on shells without pipefail.
set -eu
umask 077

case "${LOCAL_CLUSTER-}" in
    radplanes-local-shared-control|radplanes-local-shared-data|radplanes-local-isolated-1-control|radplanes-local-isolated-1-data) ;;
    *) echo "Only reserved local child nodes are permitted" >&2; exit 1 ;;
esac
[ -n "${LOCAL_IMAGES-}" ] || { echo "No application images supplied" >&2; exit 1; }

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
    rm -f "$work/stream" "$work/images" "$work/label" "$work/role" "$work/loaded" "$work/cancelled"
    rmdir "$work"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

printf '%s\n' "$LOCAL_IMAGES" > "$work/images"
count=0
while IFS= read -r image; do
    case "$image" in
        localhost/radplanes-plane-api:*|localhost/radplanes-plane-provisioner:*) ;;
        *) echo "Unapproved application image" >&2; exit 1 ;;
    esac
    tag=${image##*:}
    case "$tag" in
        ''|*[!0-9a-f]*) echo "Image tags must be lowercase source hashes" >&2; exit 1 ;;
    esac
    [ "${#tag}" -ge 40 ] && [ "${#tag}" -le 64 ] || {
        echo "Image tag length is not a source hash" >&2; exit 1
    }
    count=$((count + 1))
done < "$work/images"
[ "$count" -le 2 ] || { echo "At most two application images are permitted" >&2; exit 1; }

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
[ "$label" = "$LOCAL_CLUSTER" ] && [ "$role" = control-plane ] || {
    echo "Docker child ownership labels mismatch" >&2; exit 1
}
mkfifo "$work/stream"
while IFS= read -r image; do
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
    [ "$saved" -eq 0 ] && [ "$imported" -eq 0 ] || {
        echo "Image stream failed: save=$saved import=$imported" >&2; exit 1
    }
    docker exec "$node" ctr --namespace k8s.io images list --quiet > "$work/loaded" &
    consumer=$!
    wait "$consumer"
    consumer=
    grep -F -x -- "$image" "$work/loaded" >/dev/null || {
        echo "Imported image reference is absent from the child" >&2; exit 1
    }
done < "$work/images"
