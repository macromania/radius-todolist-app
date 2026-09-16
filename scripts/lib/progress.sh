#!/usr/bin/env bash
set -euo pipefail
set +x
# shellcheck source=output.sh
source "$(dirname "${BASH_SOURCE[0]}")/output.sh"
(( $# >= 2 )) || { demo_status error 'Usage: progress.sh LABEL COMMAND [ARGUMENT...]'; exit 2; }
demo_run "$@"
