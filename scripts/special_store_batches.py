"""Batch adapters for stores with their own observation/history contracts."""
from types import SimpleNamespace

from scripts import refresh_kaufmann_inventory as kaufmann
from scripts import refresh_weekday_database as weekday

SPECS = {
    'kaufmann': SimpleNamespace(key='kaufmann', table='kaufmann_products'),
    'weekday': weekday.SPEC,
}


def read_rows(database, store, *, include_unavailable=False):
    if store == 'kaufmann':
        rows = kaufmann.SupabaseKaufmannRefreshClient.list_existing_variants(
            database, SPECS[store].table, include_unavailable=include_unavailable)
        for row in rows:
            row['canonical_url'] = kaufmann.clean_product_url(row.get('canonical_url') or row['source_url'])
        return rows
    return weekday.list_rows(database, include_unavailable=include_unavailable)


class KaufmannStage:
    def __init__(self, staged):
        self.staged = staged

    def list_existing_variants(self, *args, **kwargs):
        return self.staged.rows

    def upsert_products_by_id(self, table, rows):
        if table != SPECS['kaufmann'].table:
            raise ValueError('Unexpected Kaufmann table')
        for row in rows:
            # Error-only updates do not constitute a completed observation.
            if 'updated_at' in row:
                self.staged.patch_row(SPECS['kaufmann'], row['id'],
                    {key: value for key, value in row.items() if key in kaufmann.DYNAMIC_PRODUCT_COLUMNS})

    def insert_history_observations(self, table, rows):
        for row in rows:
            self.staged.queue_history_observation(row)

    def create_refresh_run(self, *args):
        pass  # The scheduler owns the single whole-cycle operational record.

    def update_refresh_run(self, *args):
        pass

    def close(self):
        pass


def run_kaufmann(staged, args, *, include_unavailable=False):
    from scripts.refresh_store_inventory import RefreshStats
    options = kaufmann.parse_args([])
    options.include_unavailable = include_unavailable
    options.no_delay = args.no_delay
    result = {}
    kaufmann.refresh_kaufmann_inventory(options, client_override=KaufmannStage(staged), result_out=result)
    stats = RefreshStats(pages=result['pages'], updated=len(staged.patches),
        unavailable=result['unavailable'], history_observations=len(staged.history), failed=result['failed'])
    return stats, result['candidates']


def run(staged, args):
    if staged.store == 'kaufmann':
        return run_kaufmann(staged, args)[0]
    return weekday.refresh_store(staged, args, existing_rows=staged.rows, stop_on_block=True)


def completed_ids(store, patches):
    return {patch['id'] for patch in patches
            if store != 'weekday' or 'inventory_checked_at' in patch['payload']}
