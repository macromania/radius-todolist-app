#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .state/build
uv export --no-dev --no-hashes --no-emit-project --format requirements-txt \
  --output-file .state/build/api-constraints.txt --quiet
uv pip compile containers/provisioner/requirements.in \
  --constraint .state/build/api-constraints.txt --python-version 3.13 \
  --python-platform x86_64-unknown-linux-gnu \
  --output-file containers/provisioner/requirements.txt --quiet
uv pip compile containers/provisioner/azure-cli.in --python-version 3.13 \
  --python-platform x86_64-unknown-linux-gnu --prerelease=allow \
  --output-file containers/provisioner/azure-cli.txt --quiet
