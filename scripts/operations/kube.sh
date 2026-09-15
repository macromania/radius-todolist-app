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
  printf '%s\n' 'Usage: kube SLOT kubectl-arguments...' \
    'Uses fresh access to the selected cluster and application namespace.'
  exit 0
fi
(( $# >= 2 )) || { demo_error 'Usage: kube SLOT kubectl-arguments...'; exit 1; }
slot=$1
shift
for argument in "$@"; do
  case "$argument" in
    --) break ;;
    --kubeconfig*|--context*|--namespace*|-n|-n?*|--server*|-s|-s?*|--cluster*|--user*|\
    --token*|--certificate-authority*|--client-certificate*|--client-key*|\
    --insecure-skip-tls-verify*|--tls-server-name*|--proxy-url*)
      demo_error 'Connection and namespace overrides are not supported'
      exit 1 ;;
  esac
done
demo_load_env "$ROOT/.env"
demo_workspace
trap demo_remove_workspace EXIT
demo_open_slot "$slot"
demo_kube "$@"
