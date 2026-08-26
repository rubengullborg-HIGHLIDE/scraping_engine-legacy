#!/usr/bin/env bash
set -euo pipefail

TIMERS=(
  highlide-kaufmann-refresh.timer
  highlide-kaufmann-weekly-sweep.timer
  highlide-store-refresh.timer
  highlide-store-catalog-sync.timer
)

SERVICES=(
  highlide-kaufmann-refresh.service
  highlide-kaufmann-weekly-sweep.service
  highlide-store-refresh.service
  highlide-store-catalog-sync.service
)

echo "Stopping and disabling HIGHLIDE continual scraping timers..."
sudo systemctl disable --now "${TIMERS[@]}"

echo
echo "No future continual scraping runs will be scheduled."
echo "Any scraper service already running has been left to finish safely."
echo
echo "Current scraper service state:"
for service in "${SERVICES[@]}"; do
  printf '%-48s %s\n' "$service" "$(systemctl is-active "$service" 2>/dev/null || true)"
done

