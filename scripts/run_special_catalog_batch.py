"""One journaled Kaufmann/Weekday catalog batch, called under the shared lock."""
from __future__ import annotations
import argparse
import json
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.catalog_manifest import save_state
from scripts import special_store_batches as special
from scripts.run_inventory_batches import BatchInventoryClient, StagedClient, now
from scripts import sync_store_catalogs as sync


def report_path(manifest, offset):
    return manifest.with_name(f'{manifest.stem}-batch-{offset}.json')


def discover(store, rows):
    known = {row['canonical_url'] for row in rows}
    if store == 'kaufmann':
        from scripts.sync_kaufmann_catalog import KaufmanScraper
        scraper = KaufmanScraper(launch_browser=False)
        try:
            urls = set(scraper.discover_product_links_from_sitemap('https://www.kaufmann.dk/sitemap.xml'))
            if not urls or (len(known) >= 10 and len(urls) < len(known) * .5):
                raise ValueError('Kaufmann sitemap coverage guard failed')
            return sorted(urls | known)
        finally:
            scraper.close()
    from scrapers.full_import.weekday import Client, discover_catalog, canonical_url, in_scope
    client = Client(delay=3)
    try:
        discovery = discover_catalog(client)
        if not discovery.get('complete'):
            raise ValueError('Incomplete Weekday discovery')
        products = [p for p in discovery['products'] if in_scope(p)]
        urls = {canonical_url(p) for p in products}
        urls.update(canonical_url(v) for p in products for v in p.get('colorVariants', []))
        if not urls:
            raise ValueError('Empty Weekday discovery')
        return sorted(urls | known)
    finally:
        client.close()


def prepare(store, urls, rows):
    selected = [row for row in rows if row['canonical_url'] in urls]
    staged = StagedClient(store, selected)
    args = SimpleNamespace(no_delay=False, dry_run=False, no_history=False, offset=0, limit=None)
    known = {row['canonical_url'] for row in rows}
    new_urls = set(urls) - known
    new_rows = []
    if store == 'kaufmann':
        stats, candidates = special.run_kaufmann(staged, args, include_unavailable=True)
        if stats.failed:
            raise ValueError('Incomplete Kaufmann sweep; retry the batch without advancing missing-colour counters')
        from scripts.sync_kaufmann_catalog import KaufmanScraper, KaufmannVariationClient, available_catalog_rows
        from scrapers.stores.kaufmann_variations import KaufmannVariationUnavailable
        scraper = KaufmanScraper(launch_browser=False)
        variations = KaufmannVariationClient()
        keys = {(row['source_parent_id'], row['source_color_id']) for row in rows}
        try:
            for index, url in enumerate(sorted(new_urls | set(candidates))):
                if index:
                    time.sleep(3)
                try:
                    imported = available_catalog_rows(scraper, variations, url)
                except KaufmannVariationUnavailable:
                    if url not in new_urls:
                        raise
                    # A stale sitemap entry with a confirmed 404/410 has no row
                    # to import; known products were handled by the sweep above.
                    continue
                new_rows.extend(row for row in imported
                                if (row['source_parent_id'], row['source_color_id']) not in keys)
        finally:
            scraper.close()
            variations.close()
    else:
        stats = special.run(staged, args)
        if stats.failed or stats.skipped:
            raise ValueError('Incomplete Weekday sweep; batch retained')
        from scrapers.full_import.weekday import Client, in_scope, catalog_row
        from scripts.weekday_database import database_row, exclusion_reasons
        client = Client(delay=3)
        try:
            for url in sorted(new_urls):
                product = client.fetch_detail(url)
                if not in_scope(product):
                    continue
                inventory = client.fetch_inventory(product)
                checked = now()
                row, flags = catalog_row(product, inventory, url, checked)
                if flags:
                    raise ValueError('Incomplete Weekday catalog inventory; batch retained')
                row.update(price_observed_at=checked, inventory_observed_at=checked,
                           inventory_complete=True, inventory_warnings=[])
                if row['aarhus_available'] is True:
                    reasons = exclusion_reasons(row)
                    if reasons:
                        raise ValueError('Ineligible Weekday catalog product: ' + ', '.join(reasons))
                    new_rows.append(database_row(row))
        finally:
            client.close()
    # Linked source URLs can resolve to the same colour.
    unique = {(row['source_parent_id'], row['source_color_id']): row for row in new_rows}
    return dict(patches=staged.patches, history=staged.history, new_rows=list(unique.values()), done=False)


def apply(database, store, plan):
    spec = special.SPECS[store]
    for patch in plan['patches']:
        database.patch_row(spec, patch['id'], patch['payload'])
    for entry in plan['history']:
        database.queue_history_observation(entry)
    database.flush_history()
    current = special.read_rows(database, store, include_unavailable=True)
    by_key = {(row['source_parent_id'], row['source_color_id']): row for row in current}
    for row in plan['new_rows']:
        existing = by_key.get((row['source_parent_id'], row['source_color_id']))
        fields = ('updated_at',)
        if existing and any(sync.parse_timestamp(existing.get(field)) > sync.parse_timestamp(row.get(field)) for field in fields):
            raise ValueError('Newer catalog row blocks stale import replay')
        response = database.session.post(database._table_url(spec.table),
            params={'on_conflict':'source_parent_id,source_color_id'},
            headers={'Prefer':'resolution=merge-duplicates,return=minimal'}, json=row,
            timeout=database.timeout_seconds)
        response.raise_for_status()
    touched = {patch['id'] for patch in plan['patches']}
    new_keys = {(row['source_parent_id'], row['source_color_id']) for row in plan['new_rows']}
    observed = {}
    for row in special.read_rows(database, store, include_unavailable=True):
        if row['id'] in touched or (row['source_parent_id'], row['source_color_id']) in new_keys:
            observed[str(row['id'])] = row['updated_at']
    return observed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True, choices=list(special.SPECS))
    parser.add_argument('--catalog-manifest', required=True, type=Path)
    parser.add_argument('--offset', type=int, required=True)
    parser.add_argument('--limit', type=int, required=True)
    args = parser.parse_args(argv)
    if args.offset < 0 or args.limit < 1:
        parser.error('Invalid batch bounds')
    sync.load_dotenv(ROOT / '.env')
    url, key = sync.env('SUPABASE_URL'), sync.env('SUPABASE_SECRET_KEY') or sync.env('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise ValueError('Supabase server credentials required')
    database = BatchInventoryClient(url, key,
        history_table=sync.env('STORE_INVENTORY_HISTORY_TABLE', 'store_inventory_history'),
        runs_table=sync.env('STORE_INVENTORY_REFRESH_RUNS_TABLE', 'store_inventory_refresh_runs'))
    path = report_path(args.catalog_manifest, args.offset)
    try:
        if path.exists():
            plan = json.loads(path.read_text())
            if plan['store'] != args.store or plan['limit'] != args.limit:
                raise ValueError('Catalog journal configuration mismatch')
            if plan['done']:
                return 0
        else:
            rows = special.read_rows(database, args.store, include_unavailable=True)
            if args.catalog_manifest.exists():
                manifest = json.loads(args.catalog_manifest.read_text())
            else:
                manifest = dict(version=1, store=args.store, items=discover(args.store, rows))
                save_state(args.catalog_manifest, manifest)
            if manifest.get('version') != 1 or manifest.get('store') != args.store or not manifest['items']:
                raise ValueError('Invalid catalog discovery manifest')
            plan = prepare(args.store, manifest['items'][args.offset:args.offset + args.limit], rows)
            plan.update(store=args.store, limit=args.limit)
            save_state(path, plan)
        plan['observed'] = apply(database, args.store, plan)
        plan['done'] = True
        save_state(path, plan)
        return 0
    finally:
        database.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    raise SystemExit(main())
