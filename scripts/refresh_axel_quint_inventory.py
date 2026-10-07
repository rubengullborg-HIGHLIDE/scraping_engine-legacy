"""Existing-row refresh for AXEL/qUINT, with a store-wide missing-source guard."""
from __future__ import annotations

import logging
from collections import OrderedDict

from scrapers.full_import.axel_quint import AxelQuintScraper, SourceUnavailable

LOG = logging.getLogger(__name__)


def refresh_store(store, database, args):
    # Imported at call time to avoid circular registry imports.
    from scripts.refresh_store_inventory import (
        STORE_SPECS, RefreshStats, apply_full_row, apply_unavailable,
        dynamic_payload, maybe_delay, row_url, selected_rows,
    )
    spec = STORE_SPECS[store]
    rows = selected_rows(database, spec, args)
    stats = RefreshStats()
    groups = OrderedDict()
    for row in rows:
        groups.setdefault(row_url(row), []).append(row)
    LOG.info('Loaded %s %s rows across %s pages.', len(rows), store, len(groups))
    scraper = AxelQuintScraper(store, max_retries=args.max_retries)
    staged, missing = [], []
    try:
        for index, (url, existing) in enumerate(groups.items(), 1):
            maybe_delay(index, args)
            stats.pages += 1
            LOG.info('%s [%s/%s] Refreshing %s', store, index, len(groups), url)
            try:
                current = scraper.scrape(url, full=False)
                parents = {r['source_parent_id'] for r in current}
                if len(parents) != 1 or any(r['source_parent_id'] not in parents for r in existing):
                    raise ValueError('Source parent changed; refusing to patch or discontinue existing rows')
                by_key = {(r['source_parent_id'], r['source_color_id']): r for r in current}
                for row in existing:
                    match = by_key.get((row['source_parent_id'], row['source_color_id']))
                    if match is None:
                        missing.append((row, 'source_item_missing'))
                    else:
                        # Validate all dynamic fields before any write; omit metadata/raw.
                        staged.append((row, {**dynamic_payload(spec, match),
                                             'source_color_id': match['source_color_id']}))
            except SourceUnavailable:
                missing.extend((row, 'page_unavailable') for row in existing)
            except Exception:
                stats.failed += len(existing)
                LOG.exception('Failed %s page %s; existing data retained.', store, url)
        if missing and (not staged or stats.failed or (len(missing) >= 3 and len(missing) / len(rows) >= .2)):
            raise RuntimeError(f'{store} safety stop: {len(missing)}/{len(rows)} rows missing, {stats.failed} failed. No updates written.')
        for row, current in staged:
            apply_full_row(spec, database, row, current, args, stats)
        for row, status in missing:
            apply_unavailable(spec, database, row, args, stats, refresh_status=status)
    finally:
        scraper.close()
    return stats


def main(store):
    import sys
    from scripts.refresh_store_inventory import parse_args, refresh_stores
    args = parse_args(['--store', store, *sys.argv[1:]])
    if args.store != [store]:
        raise ValueError('Dedicated refresh entry point accepts only its own store')
    logging.basicConfig(level=args.log_level, format='%(asctime)s %(levelname)s %(message)s')
    return refresh_stores(args)
