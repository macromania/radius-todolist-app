#!/usr/bin/env bash
set -euo pipefail
set +x
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=../lib/env.sh
source "$ROOT/scripts/lib/env.sh"
if [[ "${1:-}" == --help ]]; then
  printf '%s\n' 'Usage: fault-status SLOT COMPONENT' \
    'Read the selected Kubernetes fault journal without changing the fault.'
  exit 0
fi
(( $# == 2 )) || { demo_error 'Usage: fault-status SLOT COMPONENT'; exit 1; }
slot=$1 component=$2
case "$slot:$component" in
  shared-control:control-reconciler|isolated-1-control:control-reconciler|\
  shared-data:data-reconciler|isolated-1-data:data-reconciler) ;;
  *) demo_error 'Choose a matching control/data slot and reconciler'; exit 1 ;;
esac
bash "$ROOT/scripts/operations/kube.sh" "$slot" get configmap \
  "plane-demo-fault-$component" -o json |
  jq -e --arg slot "$slot" --arg component "$component" '
    if .kind != "ConfigMap" or .metadata.name != ("plane-demo-fault-" + $component)
    then error("Unexpected fault journal resource")
    else .data["record.json"] | fromjson
    end |
    if type != "object" then error("Invalid fault journal record")
    elif .slot != $slot or .component != $component then error("Fault journal target differs")
    else .
    end
  '
