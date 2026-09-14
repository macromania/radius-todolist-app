#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=../lib/env.sh
source "$ROOT/scripts/lib/env.sh"
# shellcheck source=../lib/discovery.sh
source "$ROOT/scripts/lib/discovery.sh"
if [[ "${1:-}" == --help ]]; then
  printf '%s\n' 'Usage: endpoints [management|shared-control|shared-data|isolated-1-control|isolated-1-data|all]'
  exit 0
fi
(( $# <= 1 )) || { demo_error 'Provide one endpoint slot or all'; exit 1; }
demo_load_env "$ROOT/.env"
demo_workspace
trap demo_remove_workspace EXIT
if [[ "${1:-management}" == all ]]; then
  slots=(management shared-control shared-data isolated-1-control isolated-1-data)
else
  slots=("${1:-management}")
fi
for slot in "${slots[@]}"; do
  demo_open_slot "$slot"
  url=$(demo_endpoint)
  jq -n --arg slot "$slot" --arg url "$url" '{slot:$slot,url:$url}'
done
