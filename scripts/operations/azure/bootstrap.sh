#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/operations/azure/bootstrap.py" "$@"
