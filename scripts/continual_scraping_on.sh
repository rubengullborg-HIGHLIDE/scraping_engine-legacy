#!/usr/bin/env bash
set -euo pipefail

TIMERS=(
  highlide-kaufmann-refresh.timer
  highlide-kaufmann-weekly-sweep.timer
  highlide-store-refresh.timer
  highlide-store-catalog-sync.timer
)

echo "Enabling and starting HIGHLIDE continual scraping timers..."
sudo systemctl enable --now "${TIMERS[@]}"

echo
echo "HIGHLIDE timers are enabled. Upcoming runs:"
systemctl list-timers --all "${TIMERS[@]}" --no-pager

