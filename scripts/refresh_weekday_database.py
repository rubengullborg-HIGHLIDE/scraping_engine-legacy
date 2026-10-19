"""Daily Weekday adapter: patch existing Aarhus rows, with staged stock guards."""
import logging
from types import SimpleNamespace

from scrapers.full_import.weekday import Client
from scripts.refresh_weekday_inventory import (
    PRICE_FIELDS, INVENTORY_FIELDS, observe, apply_observations,
)

LOG = logging.getLogger(__name__)
SPEC = SimpleNamespace(key='weekday', table='weekday_products')


def list_rows(database, *, include_unavailable=False):
    columns = ('id', 'source_parent_id', 'source_color_id', 'canonical_url',
               'inventory_checked_at', 'updated_at',
               *sorted((PRICE_FIELDS | INVENTORY_FIELDS) - {'price_observed_at', 'inventory_observed_at'}))
    rows = []
    while True:
        start = len(rows)
        response = database.session.get(
            database._table_url(SPEC.table),
            params={'select': ','.join(columns),
                    **({} if include_unavailable else {'aarhus_available': 'eq.true'}),
                    'order': 'inventory_checked_at.asc,id.asc'},
            headers={'Range': f'{start}-{start + 999}'}, timeout=database.timeout_seconds,
        )
        response.raise_for_status()
        batch = response.json()
        rows.extend(batch)
        if len(batch) < 1000:
            return rows


def refresh_store(database, args, *, existing_rows=None, stop_on_block=False):
    from scripts.refresh_store_inventory import RefreshStats, history_payload

    rows = (list_rows(database) if existing_rows is None else existing_rows)[args.offset:]
    if args.limit:
        rows = rows[:args.limit]
    stats = RefreshStats()
    LOG.info('Selected %s Aarhus-available Weekday rows.', len(rows))
    if not rows:
        return stats
    # Detail + inventory per known colour avoids a broad daily catalogue scan.
    client = Client(delay=3)
    results = []
    try:
        for index, row in enumerate(rows, 1):
            result = observe(row, client)
            results.append(result)
            stats.pages += 1
            LOG.info('weekday [%s/%s] %s: %s', index, len(rows), row['source_color_id'], result['status'])
            if result['status'] == 'blocked':
                LOG.warning('Weekday source blocked; stopping requests and retaining unverified stock.')
                break
    finally:
        client.close()

    if stop_on_block and any(result['status'] == 'blocked' for result in results):
        from scrapers.source_http import StoreRateLimited
        error = StoreRateLimited('Weekday blocked; defer remaining batches')
        error.retry_after_seconds = 86400
        raise error

    # Validate the whole selected sample before persisting any inventory, so an
    # empty/broken upstream response cannot erase most of the stored catalogue.
    updated, guards = apply_observations(rows, rows, results)
    for reason in guards:
        LOG.warning('Weekday inventory guard: %s', reason)
    stats.skipped = len(rows) - len(results)
    for existing, current, result in zip(rows, updated, results):
        fields = set(result['applied_fields']) & (PRICE_FIELDS | INVENTORY_FIELDS)
        patch = {field: current[field] for field in fields}
        if result['status'] != 'success' or guards:
            stats.failed += 1
            LOG.warning('Weekday %s: %s', existing['source_color_id'],
                        '; '.join(result['warnings'] + result['errors'] + guards))
            continue
        if not patch:
            continue
        checked = min(patch.pop('price_observed_at'), patch.pop('inventory_observed_at'))
        patch.update(inventory_checked_at=checked, updated_at=checked, scraped_at=checked)
        if not args.dry_run:
            database.patch_row(SPEC, existing['id'], patch)
            # Never record retained old inventory as a new stock observation.
            if not args.no_history:
                database.queue_history_observation(history_payload(
                    SPEC, existing, current, checked_at=checked,
                    refresh_status='ok',
                ))
                stats.history_observations += 1
        stats.updated += 1
    return stats
