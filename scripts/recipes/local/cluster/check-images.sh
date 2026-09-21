#!/bin/sh
# This data source runs before kind creation. Docker inspect never pulls missing images.
set -eu
# shellcheck source=image-contract.sh
. "$(dirname "$0")/image-contract.sh"
prefix=${1-}
check_prefix "$prefix" || { echo "Invalid selected image prefix" >&2; exit 1; }
shift
if [ "$#" -lt 2 ] || [ "$#" -gt 128 ] || [ "$(($# % 2))" -ne 0 ]; then
    echo "Expected bounded reference/image-ID pairs" >&2; exit 1
fi
while [ "$#" -gt 0 ]; do
    check_image "$prefix" "$1" || { echo "Unapproved prepared image" >&2; exit 1; }
    id=${2#sha256:}
    case "$id" in ''|*[!a-f0-9]*) echo "Invalid prepared image ID" >&2; exit 1 ;; esac
    [ "$2" != "$id" ] && [ "${#id}" -eq 64 ] || exit 1
    actual=$(docker image inspect --format '{{.Id}}' "$1")
    [ "$actual" = "$2" ] || { echo "Prepared Docker image changed or is missing" >&2; exit 1; }
    shift 2
done
printf '{"prepared":"true"}\n'
