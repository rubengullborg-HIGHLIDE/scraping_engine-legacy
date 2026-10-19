"""Explicit full-import writes; never used by the inventory refresh path."""
from pathlib import Path
from urllib.parse import urlsplit

from scripts.import_rains_products import SupabaseCatalogClient, env, load_dotenv
from scrapers.full_import.weekday import require
from scripts.weekday_local import atomic_json, now

TABLE = 'weekday_products'
FIELDS = (
    'source_parent_id', 'source_color_id', 'canonical_url', 'name', 'brand', 'color',
    'category_path', 'description_html', 'images', 'material', 'fit',
    'current_price', 'list_price', 'currency', 'local_inventory', 'local_total_stock',
    'local_available', 'aarhus_total_stock', 'aarhus_available', 'scraped_at',
    'inventory_complete', 'inventory_warnings',
)


def exclusion_reasons(row):
    """Fail closed on missing images or any unknown/inconsistent physical stock."""
    reasons = []
    images = row.get('images')
    if not isinstance(images, list) or not images or not all(
        isinstance(url, str) and urlsplit(url).scheme == 'https' and urlsplit(url).hostname
        for url in images
    ):
        reasons.append('missing_or_invalid_images')
    if row.get('aarhus_available') is not True:
        reasons.append('not_aarhus_available')
    store = (row.get('local_inventory') or {}).get('stores', {}).get('weekday-aarhus', {})
    sizes = store.get('sizes') or {}
    known = bool(sizes) and all(
        isinstance(v, dict) and type(v.get('stock')) is int and v['stock'] >= 0
        and v.get('available') is (v['stock'] > 0) for v in sizes.values()
    )
    total = sum(v['stock'] for v in sizes.values()) if known else None
    expected_labels = set((row.get('source_sizes') or {}).values())
    if not (row.get('inventory_complete') is True and row.get('inventory_warnings') == []
            and store.get('stock_known') is True and store.get('available') is True
            and known and total > 0 and set(sizes) == expected_labels
            and all(type(v) is int and v == total for v in
                    (store.get('total_stock'), row.get('aarhus_total_stock'), row.get('local_total_stock')))
            and row.get('local_available') is True):
        reasons.append('incomplete_or_inconsistent_aarhus_inventory')
    return reasons


def catalog_eligibility(products):
    skipped = []
    for row in products:
        reasons = exclusion_reasons(row)
        if reasons:
            skipped.append({'source_color_id': row['source_color_id'], 'reasons': reasons})
    return {'eligible': len(products) - len(skipped), 'excluded': len(skipped), 'skipped': skipped}


def database_row(row):
    reasons = exclusion_reasons(row)
    require(not reasons, 'Product is not eligible for database import: ' + ', '.join(reasons))
    result = {key: row[key] for key in FIELDS}
    checked = min(row['price_observed_at'], row['inventory_observed_at'])
    result.update(inventory_checked_at=checked, updated_at=checked)
    result['raw'] = {key: row[key] for key in ('source_sizes', 'source_metadata', 'source_inventory')}
    return result


def connect():
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    url = env('SUPABASE_URL')
    key = env('SUPABASE_SECRET_KEY') or env('SUPABASE_SERVICE_ROLE_KEY')
    require(url and key, '--write requires SUPABASE_URL and SUPABASE_SECRET_KEY (or SUPABASE_SERVICE_ROLE_KEY)')
    client = SupabaseCatalogClient(url, key)
    try:
        response = client.session.get(f'{client.supabase_url}/rest/v1/{TABLE}',
                                      params={'select': 'id', 'limit': 0}, timeout=30)
        require(response.ok, f'Weekday table preflight failed (HTTP {response.status_code}); apply the migration and check the service key')
    except Exception:
        client.session.close()
        raise
    return client


def write_catalog(report, client, directory, batch_size=50):
    require(report.get('scan_complete') is True, 'Incomplete/failed scans cannot write to Supabase')
    require(batch_size > 0, 'Batch size must be positive')
    eligibility = catalog_eligibility(report['products'])
    rows = [database_row(row) for row in report['products'] if not exclusion_reasons(row)]
    identities = {(row['source_parent_id'], row['source_color_id']) for row in rows}
    require(len(identities) == len(rows), 'Duplicate catalog identities')
    progress = {'table': TABLE, 'started_at': now(), 'requested': len(rows),
                'confirmed_written': 0, 'complete': False, 'limited': report['limited'],
                'source_products': len(report['products']), 'eligibility': eligibility}
    path = directory / 'database-write.json'
    atomic_json(path, progress)
    for offset in range(0, len(rows), batch_size):
        batch = rows[offset:offset + batch_size]
        # Stable source identity prevents duplicates on an interrupted/repeated run.
        client.upsert_products(TABLE, batch)
        progress['confirmed_written'] += len(batch)
        atomic_json(path, progress)
        print(f"Supabase: {progress['confirmed_written']}/{len(rows)} Weekday rows upserted", flush=True)
    progress.update(complete=True, completed_at=now())
    atomic_json(path, progress)
    return progress
