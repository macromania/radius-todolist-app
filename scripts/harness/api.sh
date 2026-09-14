#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
exec uv run --python 3.13 python scripts/harness/api.py "$@"
