#!/usr/bin/env bash
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p logs

candidate_file="$(mktemp /tmp/highlide-kaufmann-new-variants.XXXXXX.json)"
trap 'rm -f "$candidate_file"' EXIT

status=0
.venv/bin/python scripts/refresh_kaufmann_inventory.py \
  --include-unavailable \
  --new-variant-candidates-output "$candidate_file" || status=$?
.venv/bin/python scripts/sync_kaufmann_catalog.py \
  --new-variant-candidates "$candidate_file" \
  "$@" || status=$?

exit "$status"
