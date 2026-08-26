#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p logs

exec .venv/bin/python scripts/sync_store_catalogs.py "$@"
