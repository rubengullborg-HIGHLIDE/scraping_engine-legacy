"""Batch adapters for stores with their own observation contracts.

Weekday refreshes through its own staged guards. Kaufmann refreshes through
the shared path but keeps a journaled weekly catalog sweep, so both stores
use `run_special_catalog_batch.py` instead of generic catalog reconciliation.
"""
from scripts import refresh_store_inventory as refresh
from scripts import refresh_weekday_database as weekday
from scrapers.stores.kaufmann_variations import clean_product_url

REFRESH_SPECS = {'weekday': weekday.SPEC}
CATALOG_SPECS = {'kaufmann': refresh.STORE_SPECS['kaufmann'], 'weekday': weekday.SPEC}


def read_rows(database, store, *, include_unavailable=False):
    if store == 'kaufmann':
        rows = refresh.SupabaseInventoryClient.list_rows(
            database, CATALOG_SPECS[store], include_unavailable=include_unavailable)
        # Catalog manifests hold sitemap URLs; compare them without query or fragment.
        for row in rows:
            row['canonical_url'] = clean_product_url(row.get('canonical_url') or row['source_url'])
        return rows
    return weekday.list_rows(database, include_unavailable=include_unavailable)


def run(staged, args):
    return weekday.refresh_store(staged, args, existing_rows=staged.rows, stop_on_block=True)


def completed_ids(store, patches):
    return {patch['id'] for patch in patches
            if store != 'weekday' or 'inventory_checked_at' in patch['payload']}
