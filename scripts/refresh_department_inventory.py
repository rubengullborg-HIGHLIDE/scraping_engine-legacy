"""Refresh department-store prices and inventory, grouped by existing style.

Absent/incomplete source data is a failure, not evidence of zero stock.
Catalog lifecycle reconciliation handles disappearance after successful imports.
"""
from __future__ import annotations
import logging
from collections import OrderedDict
from scrapers.full_import.department_stores import make_scraper, SourceUnavailable

LOG = logging.getLogger(__name__)


def refresh_store(store, database, args):
    from scripts.refresh_store_inventory import (
        STORE_SPECS, RefreshStats, selected_rows, row_url, maybe_delay,
        dynamic_payload, apply_full_row,
    )
    spec = STORE_SPECS[store]
    rows = selected_rows(database, spec, args)
    stats = RefreshStats()
    groups = OrderedDict()
    for row in rows:
        groups.setdefault(row['source_parent_id'], []).append(row)
    LOG.info('Loaded %s %s rows across %s styles.', len(rows), store, len(groups))
    scraper = make_scraper(store, max_retries=args.max_retries)
    try:
        for index, (parent, existing) in enumerate(groups.items(), 1):
            maybe_delay(index, args)
            stats.pages += 1
            LOG.info('%s [%s/%s] Refreshing style %s (%s rows).', store, index, len(groups), parent, len(existing))
            candidates = list(dict.fromkeys(row_url(r) for r in existing))
            # Salling's URLs identify sizes; another known size can survive a 404.
            if store == 'salling':
                candidates += [v['url'] for r in existing for v in r.get('webshop_sizes', []) if v.get('url') and v['url'] not in candidates]
            try:
                current = None
                for url in candidates:
                    try:
                        current = scraper.scrape(url, full=False)
                        break
                    except SourceUnavailable:
                        continue
                if not current or any(r['source_parent_id'] != parent for r in current):
                    raise ValueError('Missing or changed source style; retain existing data')
                by_key = {r['source_color_id']: r for r in current}
                batch = []
                for row in existing:
                    match = by_key.get(row['source_color_id'])
                    if match is None:
                        raise ValueError('Known colour missing; retain existing style until catalog reconciliation')
                    batch.append((row, {**dynamic_payload(spec, match), 'source_color_id': match['source_color_id']}))
            except Exception:
                stats.failed += len(existing)
                LOG.exception('Failed %s style %s; existing data retained.', store, parent)
                continue
            # Commit each completely validated style. A multi-hour refresh keeps
            # useful progress if interrupted; database failures still propagate.
            for row, current in batch:
                apply_full_row(spec, database, row, current, args, stats)
    finally:
        scraper.close()
    return stats


def main(store):
    import sys
    from scripts.refresh_store_inventory import parse_args, refresh_stores
    args = parse_args(['--store', store, *sys.argv[1:]])
    if args.store != [store]:
        raise ValueError('Dedicated refresh accepts only its own store')
    logging.basicConfig(level=args.log_level, format='%(asctime)s %(levelname)s %(message)s')
    return refresh_stores(args)
