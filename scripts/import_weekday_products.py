"""Import the complete online men's clothing discovery set, retaining Aarhus-positive rows locally.

Local JSON by default; --write upserts only products with images and complete
Aarhus inventory into weekday_products in Supabase.
Use a new --work-dir for a fresh observation, or --resume after interruption.
--offline replays a saved discovery/cache without Weekday requests.
"""
import argparse
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scrapers.full_import.weekday import (
    Blocked, Client, canonical_url, catalog_row, discover_catalog, in_scope,
    local_fields, product_from_response, require,
)
from scripts.weekday_local import atomic_json, cached, locked, now, read_json


def build_catalog(discovery, directory, client, *, offline=False, limit=None):
    candidates = {p['sku']: p for p in discovery['products'] if in_scope(p)}
    excluded = len(discovery['products']) - len(candidates)
    # Linked colours can remain in stores after disappearing from listing results.
    known_urls = {canonical_url(p) for p in discovery['products']}
    extra_urls = sorted({canonical_url(v) for p in candidates.values() for v in p.get('colorVariants', [])}
                        - known_urls)
    extra_errors = []
    for url in extra_urls:
        try:
            # The legacy URL format can omit the source ID, so use a URL-hash cache here.
            from scripts.weekday_local import fingerprint
            path = directory / 'linked-details' / (fingerprint(url) + '.json')
            if path.exists():
                detail = read_json(path)['value']
            else:
                require(not offline, 'Missing linked colour cache: ' + url)
                detail = client.fetch_detail(url)
                atomic_json(path, {'observed_at': now(), 'value': detail})
            if in_scope(detail):
                candidates.setdefault(detail['sku'], detail)
        except Blocked:
            raise
        except Exception as exc:
            extra_errors.append({'url': url, 'error': str(exc)})
    selected = list(candidates.values())[:limit] if limit is not None else list(candidates.values())
    results, rows = [], []
    report = {'schema_version': 1, 'mode': 'catalog', 'scope': 'Weekday storefront men’s clothing; Aarhus-positive rows',
              'started_at': now(), 'discovered': len(discovery['products']), 'excluded': excluded,
              'candidates': len(candidates), 'requested': len(selected), 'limited': limit is not None,
              'linked_errors': extra_errors, 'results': results, 'products': rows}
    for index, listed in enumerate(selected):
        sku = listed['sku']
        result = {'source_color_id': sku, 'canonical_url': canonical_url(listed)}
        try:
            # Validate a supplied discovery file just as strictly as network discovery.
            product_from_response({'statusCode': 200, 'data': {'marketCode': 'dk', 'languageCode': 'en',
                                   'product': listed}}, canonical_url(listed))
            inventory = cached(directory, 'inventory', sku, lambda: client.fetch_inventory(listed), offline)
            local, flags = local_fields(listed, inventory['value'])
            result.update(aarhus_available=local['aarhus_available'], inventory_rows=len(inventory['value']),
                          inventory_observed_at=inventory['observed_at'], warnings=flags)
            if local['aarhus_available'] is True:
                detail = cached(directory, 'details', sku, lambda: client.fetch_detail(canonical_url(listed)), offline)
                product = detail['value']
                require(product['sku'] == sku and product['productSku'] == listed['productSku'],
                        'Catalog detail changed identity')
                row, flags = catalog_row(product, inventory['value'], canonical_url(listed), inventory['observed_at'])
                row['price_observed_at'] = detail['observed_at']
                row['inventory_observed_at'] = inventory['observed_at']
                row['inventory_complete'] = not flags
                row['inventory_warnings'] = flags
                rows.append(row)
                result.update(status='partial' if flags else 'success', warnings=flags)
            else:
                result['status'] = 'unknown' if local['aarhus_available'] is None else 'not_available'
        except Blocked as exc:
            result.update(status='blocked', error=str(exc))
        except Exception as exc:
            result.update(status='error', error=f'{type(exc).__name__}: {exc}')
        results.append(result)
        if (index + 1) % 10 == 0 or result['status'] in ('error', 'blocked'):
            print(f"[{index + 1}/{len(selected)}] Aarhus rows: {len(rows)}; {result['status']}", flush=True)
            atomic_json(directory / 'catalog.json', report)
        if result['status'] == 'blocked':
            break
    report['completed_at'] = now()
    report['scan_complete'] = len(results) == len(selected) and not extra_errors and not any(
        r['status'] in ('blocked', 'error') for r in results)
    report['summary'] = {status: sum(r['status'] == status for r in results)
                         for status in ('success', 'partial', 'not_available', 'unknown', 'error', 'blocked')}
    from scripts.weekday_database import catalog_eligibility
    report['database_eligibility'] = catalog_eligibility(rows)
    atomic_json(directory / 'catalog.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--discovery-input', type=Path)
    parser.add_argument('--discover-only', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--delay', type=float, default=3)
    parser.add_argument('--write', action='store_true', help='Upsert rows with images and complete positive Aarhus stock')
    args = parser.parse_args(argv)
    if not 1 <= args.delay <= 60 or (args.limit is not None and args.limit < 1):
        parser.error('Delay must be 1–60 seconds and limit must be positive')
    if args.write and args.discover_only:
        parser.error('--write cannot be combined with --discover-only')
    client = database = None
    try:
        with locked(args.work_dir):
            if args.write:
                from scripts.weekday_database import connect
                database = connect()
            path = args.work_dir / 'discovery.json'
            if path.exists():
                require(args.resume or args.offline, 'Use --resume or a new work directory')
                discovery = read_json(path)
                if args.discovery_input:
                    require(discovery == read_json(args.discovery_input), 'Resume discovery input changed')
            elif args.discovery_input:
                discovery = read_json(args.discovery_input)
                atomic_json(path, discovery)
            else:
                require(not args.offline, 'Offline run needs saved discovery')
                client = Client(delay=args.delay)
                discovery = discover_catalog(client)
                atomic_json(path, discovery)
            require(discovery.get('complete') is True and discovery.get('mode') == 'discovery',
                    'Only a complete discovery report can seed this import')
            print(f"Discovered {len(discovery['products'])} product colours", flush=True)
            if args.discover_only:
                return 0
            if client is None and not args.offline:
                client = Client(delay=args.delay)
            report = build_catalog(discovery, args.work_dir, client, offline=args.offline, limit=args.limit)
            print(report['summary'], flush=True)
            print('Database eligibility:', {k: v for k, v in report['database_eligibility'].items()
                                            if k != 'skipped'}, flush=True)
            if database is not None:
                from scripts.weekday_database import write_catalog
                write_catalog(report, database, args.work_dir)
            if report['summary']['partial'] or report['summary']['unknown']:
                print('Inventory warnings remain; inspect catalog.json. Exit 1 does not necessarily mean the import failed.', flush=True)
            return int(not report['scan_complete'] or report['summary']['partial'] > 0 or report['summary']['unknown'] > 0)
    except Exception as exc:
        print(f'Weekday import failed: {exc}', file=sys.stderr)
        return 1
    finally:
        if client:
            client.close()
        if database is not None:
            database.session.close()


if __name__ == '__main__':
    sys.exit(main())
