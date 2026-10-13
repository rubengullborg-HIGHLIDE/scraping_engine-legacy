"""Daily Weekday adapter: patch existing Aarhus rows, with staged stock guards."""
import logging
from types import SimpleNamespace

from scrapers.full_import.weekday import Client
from scripts.refresh_weekday_inventory import (
    PRICE_FIELDS, INVENTORY_FIELDS, observe, apply_observations,
)

LOG = logging.getLogger(__name__)
SPEC = SimpleNamespace(key='weekday', table='weekday_products')


def list_rows(database):
    # Weekday has observation timestamps, not the generic publication lifecycle.
    columns = ('id', 'source_parent_id', 'source_color_id', 'canonical_url',
               *sorted(PRICE_FIELDS | INVENTORY_FIELDS))
    rows = []
    while True:
        start = len(rows)
        response = database.session.get(
            database._table_url(SPEC.table),
            params={'select': ','.join(columns), 'aarhus_available': 'eq.true',
                    'order': 'inventory_observed_at.asc,id.asc'},
            headers={'Range': f'{start}-{start + 999}'}, timeout=database.timeout_seconds,
        )
        response.raise_for_status()
        batch = response.json()
        rows.extend(batch)
        if len(batch) < 1000:
            return rows


def refresh_store(database, args):
    from scripts.refresh_store_inventory import RefreshStats, history_payload

    rows = list_rows(database)[args.offset:]
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
        if not patch:
            continue
        if not args.dry_run:
            database.patch_row(SPEC, existing['id'], patch)
            # Never record retained old inventory as a new stock observation.
            if not args.no_history and 'inventory_observed_at' in patch:
                database.queue_history_observation(history_payload(
                    SPEC, existing, current, checked_at=patch['inventory_observed_at'],
                    refresh_status='ok',
                ))
                stats.history_observations += 1
        stats.updated += 1
    return stats
