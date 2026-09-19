#!/usr/bin/env bash
set -euo pipefail
set +x
# shellcheck source=output.sh
source "$(dirname "${BASH_SOURCE[0]}")/output.sh"
if [[ "${1:-}" == --delegate ]]; then
  shift
  (( $# >= 2 )) || { demo_status error 'Usage: progress.sh --delegate LABEL COMMAND [ARGUMENT...]'; exit 2; }
  shift
  exec "$@"
fi
if [[ "${1:-}" == --summary-only ]]; then
  (( $# >= 3 )) || { demo_status error 'Usage: progress.sh --summary-only LABEL COMMAND [ARGUMENT...]'; exit 2; }
fi
(( $# >= 2 )) || { demo_status error 'Usage: progress.sh LABEL COMMAND [ARGUMENT...]'; exit 2; }
demo_run "$@"
