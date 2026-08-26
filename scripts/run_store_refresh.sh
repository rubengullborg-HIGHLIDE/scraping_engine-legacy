#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p logs

exec .venv/bin/python scripts/refresh_store_inventory.py "$@"
