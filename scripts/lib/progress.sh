#!/usr/bin/env bash
set -euo pipefail
set +x
# shellcheck source=output.sh
source "$(dirname "${BASH_SOURCE[0]}")/output.sh"
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
if [[ -t 1 && -t 2 && "${PLANE_DEMO_ACTIVITY_WRAPPED:-}" != 1 && -z "${NO_COLOR:-}" \
  && ( "${COLOR:-auto}" == auto || "${COLOR:-auto}" == always ) \
  && "${TERM:-dumb}" != dumb && -n "${TERM:-}" \
  && "${MAKEFLAGS:-}" != *jobserver* && "${MAKEFLAGS:-}" != *-j* \
  && "${MAKEFLAGS:-}" != *--jobs* \
  && -x "$root/.venv/bin/python" ]]; then
  exec "$root/.venv/bin/python" "$root/scripts/operations/terminal_activity.py" \
    bash "${BASH_SOURCE[0]}" "$@"
fi
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
