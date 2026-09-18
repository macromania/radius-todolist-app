#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=../lib/env.sh
source "$ROOT/scripts/lib/env.sh"
if [[ "${1:-}" == --help ]]; then
  printf '%s\n' 'Usage: stage build|inspect-build|bootstrap|deploy-management|preview-management|setup|clean-plan|clean|verify-clean|fault' \
    'The environment comes from .env. Mutations require CONFIRM_AZURE=yes or CONFIRM_LOCAL=yes.' \
    'Azure build accepts artifact/recovery options; fault accepts fault-helper options.'
  exit 0
fi
(( $# >= 1 )) || { demo_error 'Choose a stage; see stage --help'; exit 1; }
stage=$1
shift
case "$stage" in
  inspect-build|bootstrap|deploy-management|preview-management|setup|clean-plan|clean|verify-clean)
    (( $# == 0 )) || { demo_error 'This stage takes no extra arguments'; exit 1; } ;;
  build|fault) ;;
  *) demo_error 'Unknown stage'; exit 1 ;;
esac
demo_load_env "$ROOT/.env"
if [[ "$stage" == build && "$DEMO_ENV" == local && $# != 0 ]]; then
  demo_error 'Local build takes no extra arguments'; exit 1
fi
if [[ -n "${PLANE_DEMO_EXPECT_ENV:-}" && "$PLANE_DEMO_EXPECT_ENV" != "$DEMO_ENV" ]]; then
  demo_error 'The selected .env environment does not match this command'
  exit 1
fi
case "$stage" in
  build|bootstrap|deploy-management|setup|clean|fault)
    if [[ "$DEMO_ENV" == azure ]]; then
      [[ "${CONFIRM_AZURE:-}" == yes ]] || { demo_error 'Set CONFIRM_AZURE=yes'; exit 1; }
    else
      [[ "${CONFIRM_LOCAL:-}" == yes ]] || { demo_error 'Set CONFIRM_LOCAL=yes'; exit 1; }
    fi ;;
esac
cd "$ROOT"
demo_status section "$DEMO_ENV: $stage"
case "$DEMO_ENV:$stage" in
  azure:build) exec bash scripts/operations/azure/build.sh "$@" ;;
  azure:inspect-build) exec bash scripts/operations/azure/build.sh --inspect ;;
  azure:bootstrap) exec bash scripts/operations/azure/bootstrap.sh ;;
  azure:deploy-management) exec uv run --no-sync python scripts/operations/run-management-job.py --execute ;;
  azure:preview-management) exec uv run --no-sync python scripts/operations/run-management-job.py ;;
  azure:setup) demo_error 'Azure management deployment includes Radius registration'; exit 1 ;;
  azure:clean-plan) exec uv run --no-sync python scripts/operations/clean-azure.py ;;
  azure:clean) exec uv run --no-sync python scripts/operations/clean-azure.py --execute ;;
  azure:verify-clean) exec uv run --no-sync python scripts/operations/verify-clean.py ;;
  local:build) exec bash scripts/operations/local/build.sh build ;;
  local:inspect-build) exec bash scripts/operations/local/build.sh inspect ;;
  local:bootstrap) exec bash scripts/operations/local/bootstrap.sh ;;
  local:setup) exec bash scripts/operations/local/setup.sh apply ;;
  local:deploy-management)
    bash scripts/operations/local/setup.sh apply >/dev/null
    exec uv run --no-sync python scripts/operations/local/deploy-demo.py --execute ;;
  local:preview-management) exec uv run --no-sync python scripts/operations/local/deploy-demo.py ;;
  local:clean-plan) exec uv run --no-sync python scripts/operations/local/cleanup.py ;;
  local:clean) exec uv run --no-sync python scripts/operations/local/cleanup.py --execute ;;
  local:verify-clean) exec uv run --no-sync python scripts/operations/local/cleanup.py --verify ;;
  *:fault) exec uv run --no-sync python scripts/harness/fault-parent-link.py --execute "$@" ;;
esac
