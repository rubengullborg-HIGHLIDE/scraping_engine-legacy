"""Kaufmann adapter for the shared store refresh.

Kaufmann's public variation endpoint returns every colour of a style with its
price, webshop stock and exact stock in the three Aarhus shops. The adapter
fetches it once per style and writes each known colour through the shared
patch and history path. A known colour missing from a successful response
needs two consecutive observations before it is marked unavailable.
"""
from __future__ import annotations

import logging
import random
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrapers.source_http import StoreRateLimited
from scrapers.stores.kaufmann_variations import (
    KaufmannVariationClient,
    KaufmannVariationUnavailable,
    clean_product_url,
    variation_rows_from_payload,
)

LOG = logging.getLogger(__name__)
MISSING_COLOR_CONFIRMATIONS = 2
# Kaufmann serves thousands of styles; keep its historical polite pacing.
MIN_DELAY_SECONDS = 1.5
MAX_DELAY_SECONDS = 3.0


def pause(index, args):
    if index > 1 and not args.no_delay:
        time.sleep(random.uniform(max(args.min_delay, MIN_DELAY_SECONDS),
                                  max(args.max_delay, MAX_DELAY_SECONDS)))


def missing_color_payload(row, checked_at):
    """Count a first miss without claiming a new inventory observation."""
    return {
        'consecutive_source_misses': int(row.get('consecutive_source_misses') or 0) + 1,
        'status_checked_at': checked_at,
        'updated_at': checked_at,
    }


def refresh_store(database, args, *, candidates=None):
    """Refresh known Kaufmann colours. `candidates` collects style URLs with unknown available colours."""
    from scripts.refresh_store_inventory import (
        STORE_SPECS, RefreshStats, apply_full_row, apply_unavailable,
        retain_rate_limit_progress, selected_rows,
    )
    spec = STORE_SPECS['kaufmann']
    rows = selected_rows(database, spec, args)
    styles = OrderedDict()
    for row in rows:
        styles.setdefault(str(row['source_parent_id']), []).append(row)
    stats = RefreshStats()
    LOG.info('Loaded %s Kaufmann rows across %s styles.', len(rows), len(styles))
    client = KaufmannVariationClient(max_retries=args.max_retries)
    try:
        for index, (parent, known) in enumerate(styles.items(), 1):
            pause(index, args)
            stats.pages += 1
            url = clean_product_url(known[0].get('canonical_url') or known[0]['source_url'])
            LOG.info('kaufmann [%s/%s] Refreshing %s via parent=%s', index, len(styles), url, parent)
            checked_at = datetime.now(timezone.utc).isoformat()
            try:
                current = variation_rows_from_payload(
                    client.fetch_payload(parent, product_url=url), parent, url, checked_at=checked_at)
            except StoreRateLimited:
                raise
            except KaufmannVariationUnavailable as exc:
                source = 'variation' if '/widgets/product/variation/' in exc.url else 'product_page'
                LOG.warning('%s is gone; marking %s Kaufmann rows unavailable.', exc.url, len(known))
                for row in known:
                    apply_unavailable(spec, database, row, args, stats,
                                      status_reason=f'{source}_http_{exc.status_code}',
                                      extra={'source_available': False, 'consecutive_source_misses': 0})
                continue
            except Exception:
                stats.failed += len(known)
                LOG.exception('Failed Kaufmann style %s; existing data retained.', parent)
                continue

            by_color = {row['source_color_id']: row for row in current}
            known_colors = {str(row['source_color_id']) for row in known}
            if candidates is not None and any(
                    row['source_available'] and row['source_color_id'] not in known_colors for row in current):
                candidates.add(url)
            for row in known:
                match = by_color.get(str(row['source_color_id']))
                if match is not None:
                    apply_full_row(spec, database, row, match, args, stats)
                    continue
                payload = missing_color_payload(row, checked_at)
                if payload['consecutive_source_misses'] >= MISSING_COLOR_CONFIRMATIONS:
                    apply_unavailable(spec, database, row, args, stats, refresh_status='color_missing',
                                      status_reason='color_missing_from_variation_twice',
                                      extra={'source_available': False,
                                             'consecutive_source_misses': payload['consecutive_source_misses']})
                    continue
                LOG.warning('Kaufmann colour %s missing from parent %s (first miss).',
                            row['source_color_id'], parent)
                if not args.dry_run:
                    database.patch_row(spec, int(row['id']), payload)
    except StoreRateLimited as exc:
        retain_rate_limit_progress(exc, stats, len(rows))
        raise
    finally:
        client.close()
    return stats


if __name__ == '__main__':
    from scripts.refresh_department_inventory import main
    raise SystemExit(main('kaufmann'))
