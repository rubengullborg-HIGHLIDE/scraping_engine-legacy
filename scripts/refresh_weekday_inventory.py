"""Refresh existing Weekday rows locally, preserving catalog metadata and failed stock observations.

Input/output are JSON files for the local proof; no database or droplet access.
The output catalog can be the input to the next refresh. Each run uses a fresh
work directory, or --resume to continue its checkpoint. An incomplete run exits 1.
"""
import argparse
import copy
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scrapers.full_import.weekday import Blocked, Client, discover_catalog, local_fields, price_fields, require
from scripts.weekday_local import atomic_json, fingerprint, locked, now, read_json

PRICE_FIELDS = {'current_price', 'list_price', 'currency', 'price_observed_at'}
INVENTORY_FIELDS = {'local_inventory', 'local_total_stock', 'local_available', 'aarhus_total_stock',
                    'aarhus_available', 'inventory_observed_at', 'inventory_complete', 'inventory_warnings', 'scraped_at'}
DYNAMIC_FIELDS = PRICE_FIELDS | INVENTORY_FIELDS | {'last_refresh_attempt_at', 'refresh_status', 'status_reason'}


class BatchedPriceClient:
    """Reuse listing prices/size IDs while inventory is still fetched per article.

    Only existing input identities are exposed to the refresh runner. Reading the
    listing does not import new rows or change any catalog metadata.
    """
    def __init__(self, base, rows, discovery):
        self.base = base
        self.diagnostics = base.diagnostics
        self.products = {p['sku']: p for p in discovery['products']}
        self.url_ids = {r['canonical_url']: r['source_color_id'] for r in rows}
        require(len(self.url_ids) == len(rows), 'Duplicate canonical URLs in refresh input')
        self.timestamps = discovery['product_observed_at']
        self.detail_observed_at = None
        self.price_batch_requests = list(base.diagnostics)

    def fetch_detail(self, url):
        sku = self.url_ids[url]
        self.detail_observed_at = self.timestamps.get(sku)
        if sku in self.products:
            return self.products[sku]
        # Linked or older products can be absent from current listing results.
        self.detail_observed_at = None
        return self.base.fetch_detail(url)

    def fetch_inventory(self, product):
        return self.base.fetch_inventory(product)

    def close(self):
        self.base.close()


def observe(existing, client):
    result = {'source_parent_id': existing['source_parent_id'], 'source_color_id': existing['source_color_id'],
              'attempted_at': now(), 'patch': {}, 'warnings': [], 'errors': []}
    start = len(client.diagnostics)
    try:
        product = client.fetch_detail(existing['canonical_url'])
        require(product['sku'] == existing['source_color_id']
                and product['productSku'] == existing['source_parent_id'], 'Known product identity changed')
        try:
            result['patch'].update(price_fields(product),
                                   price_observed_at=getattr(client, 'detail_observed_at', None) or now())
        except Exception as exc:
            result['errors'].append('price: ' + str(exc))
        inventory = client.fetch_inventory(product)
        fields, flags = local_fields(product, inventory)
        result['warnings'].extend(flags)
        result['inventory_rows'] = len(inventory)
        if not flags:
            timestamp = now()
            result['patch'].update(fields, inventory_observed_at=timestamp, scraped_at=timestamp,
                                   inventory_complete=True, inventory_warnings=[])
        result['status'] = 'partial' if flags or result['errors'] else 'success'
    except Blocked as exc:
        result.update(status='blocked')
        result['errors'].append(str(exc))
    except Exception as exc:
        result['status'] = 'partial' if result['patch'] else 'error'
        result['errors'].append(f'{type(exc).__name__}: {exc}')
    result['requests'] = client.diagnostics[start:]
    return result


def apply_observations(rows, selected, results):
    """Validate the staged run and patch only known row identities; never insert."""
    keys = [(r['source_parent_id'], r['source_color_id']) for r in rows]
    require(len(set(keys)) == len(keys), 'Duplicate input identities')
    selected_ids = {(r['source_parent_id'], r['source_color_id']) for r in selected}
    complete = [r for r in results if 'local_inventory' in r['patch']]
    guard_reasons = []
    if selected and len(complete) / len(selected) < 0.5:
        guard_reasons.append('Fewer than half of selected rows received complete inventory; inventory patches withheld')
    old_by_key = dict(zip(keys, rows))
    previous_positive = [r for r in complete if old_by_key[(r['source_parent_id'], r['source_color_id'])].get('aarhus_available') is True]
    still_positive = sum(r['patch']['aarhus_available'] is True for r in previous_positive)
    if len(previous_positive) >= 10 and still_positive / len(previous_positive) < 0.5:
        guard_reasons.append('More than half of previously available rows became unavailable; inventory patches withheld')
    updated = copy.deepcopy(rows)
    by_key = dict(zip(keys, updated))
    seen = set()
    for result in results:
        key = (result['source_parent_id'], result['source_color_id'])
        require(key in by_key and key in selected_ids and key not in seen, 'Unexpected/duplicate refresh identity')
        seen.add(key)
        patch = dict(result['patch'])
        require(set(patch) <= DYNAMIC_FIELDS, 'Attempt to refresh stable metadata')
        if guard_reasons:
            patch = {k: v for k, v in patch.items() if k not in INVENTORY_FIELDS}
        patch.update(last_refresh_attempt_at=result['attempted_at'],
                     refresh_status='inventory_guard_blocked' if guard_reasons else result['status'],
                     status_reason='; '.join(guard_reasons + result['warnings'] + result['errors']) or None)
        by_key[key].update(patch)
        result['applied_fields'] = sorted(patch)
    return updated, guard_reasons


def refresh(payload, client, directory, *, limit=None, resume=False):
    require(payload.get('mode') == 'catalog', 'Expected catalog input')
    rows = payload['products']
    require(bool(rows), 'No existing rows to refresh')
    keys = [(r['source_parent_id'], r['source_color_id']) for r in rows]
    require(len(keys) == len(set(keys)), 'Duplicate input identities')
    # Oldest complete inventory first. Partial observations never move that timestamp forward.
    selected = sorted(rows, key=lambda r: (r.get('inventory_observed_at') or r.get('scraped_at') or '', r['source_color_id']))
    if limit is not None:
        selected = selected[:limit]
    checkpoint = directory / 'refresh-checkpoint.json'
    signature = fingerprint({'input': payload, 'limit': limit})
    if checkpoint.exists():
        require(resume, 'Use --resume or a new work directory')
        report = read_json(checkpoint)
        require(report['input_hash'] == signature, 'Resume input or selection changed')
        # Retry failed or blocked attempts, retaining completed price-only/complete observations.
        report['results'] = [r for r in report['results'] if r['status'] in ('success', 'partial') and not r['errors']]
    else:
        report = {'schema_version': 1, 'mode': 'refresh', 'started_at': now(), 'input_hash': signature,
                  'requested': len(selected), 'results': [],
                  'price_batch_requests': getattr(client, 'price_batch_requests', [])}
    seen = {r['source_color_id'] for r in report['results']}
    for row in selected:
        if row['source_color_id'] in seen:
            continue
        result = observe(row, client)
        report['results'].append(result)
        atomic_json(checkpoint, report)
        index = len(report['results'])
        if index % 10 == 0 or result['status'] in ('error', 'blocked'):
            print(f"[{index}/{len(selected)}] {result['status']}: {row['source_color_id']}", flush=True)
        if result['status'] == 'blocked':
            break
    updated, guards = apply_observations(rows, selected, report['results'])
    report['completed_at'] = now()
    report['inventory_guard_reasons'] = guards
    report['summary'] = {'price_updates': sum('price_observed_at' in r['applied_fields'] for r in report['results']),
                         'inventory_updates': sum('inventory_observed_at' in r['applied_fields'] for r in report['results']),
                         'statuses': {s: sum(r['status'] == s for r in report['results']) for s in ('success', 'partial', 'error', 'blocked')}}
    output = copy.deepcopy(payload)
    output.update(products=updated, last_refreshed_at=report['completed_at'])
    atomic_json(directory / 'catalog-refreshed.json', output)
    atomic_json(directory / 'refresh-report.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--per-product-prices', action='store_true',
                        help='Fetch each detail separately instead of batching current listing prices')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--delay', type=float, default=3)
    args = parser.parse_args(argv)
    if not 1 <= args.delay <= 60 or (args.limit is not None and args.limit < 1):
        parser.error('Delay must be 1–60 seconds and limit must be positive')
    client = None
    try:
        require(args.input.resolve() != (args.work_dir / 'catalog-refreshed.json').resolve(),
                'Output must not overwrite the input catalog; use a new work directory')
        with locked(args.work_dir):
            payload = read_json(args.input)
            client = Client(delay=args.delay)
            if not args.per_product_prices:
                print('Reading current Danish prices and size IDs in catalog batches', flush=True)
                discovery = discover_catalog(client)
                client = BatchedPriceClient(client, payload['products'], discovery)
            report = refresh(payload, client, args.work_dir, limit=args.limit, resume=args.resume)
            print(report['summary'], flush=True)
            return int(bool(report['inventory_guard_reasons'])
                       or report['summary']['statuses']['success'] != report['requested'])
    except Exception as exc:
        print(f'Weekday refresh failed: {exc}', file=sys.stderr)
        return 1
    finally:
        if client:
            client.close()


if __name__ == '__main__':
    sys.exit(main())
