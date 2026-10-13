#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p logs

# Validated on the droplet for all eight Shopify sources. A trailing explicit
# --source-transport requests remains available for diagnostic comparisons.
exec .venv/bin/python scripts/refresh_store_inventory.py --source-transport system-curl "$@"
