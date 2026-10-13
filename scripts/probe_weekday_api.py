"""Experimental Weekday catalog and dynamic refresh through public JSON APIs.

Install: .venv/bin/python -m pip install -r requirements-weekday.txt
Catalog: .venv/bin/python scripts/probe_weekday_api.py catalog \
    --urls-file tests/fixtures/weekday_urls.txt --output /tmp/weekday-catalog.json
Refresh: .venv/bin/python scripts/probe_weekday_api.py refresh \
    --input /tmp/weekday-catalog.json --output /tmp/weekday-refresh.json

No database access. Catalog captures metadata; refresh emits dynamic patches for
existing source identities only. The input catalog is never changed. Failures
emit diagnostics, never zero-stock updates. A nonzero exit means partial/failure.

2026-10-07 source investigation:
- HTML can return a 200 bot challenge while both public JSON endpoints work.
- Product detail advertises a 30-minute cache lifetime, online /stock 15 minutes.
  /stock is webshop-only and is deliberately not used for physical availability.
- Local inventory advertises no-cache/no-store, but has no source stock timestamp.
- Empty variantId + quantity 0 rows are store placeholders, not product variants.
- The frontend fills omitted sizes with zero for returned stores. Some responses
  have exactly 100 rows: until completeness is established, at >=100 rows omitted
  sizes stay unknown. Missing stores/empty payloads also stay unknown.
- Positive voucher percentages are rejected until effective-price rules are tested.

Endpoints and stock mapping were read from Weekday's own public frontend bundle:
https://www.weekday.com/_next/static/chunks/0phfo7maxo6wu.js
This is a bounded proof (maximum ten products), not a scheduled integration.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

# Support both `python scripts/...` and package imports used by tests.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scrapers.full_import.weekday import (  # re-export the original proof API
    PROFILE, STORE_ID, STORE_KEY, STORE_ADDRESS, POSSIBLE_LIMIT, DYNAMIC_FIELDS,
    ObservationError, Blocked, require, slug_from_url, product_from_response,
    size_map, money, price_fields, local_fields, dynamic_fields, catalog_row,
    refresh_patch, Client,
)


def read_targets(args):
    if args.mode == 'catalog':
        require(args.urls_file is not None and args.input is None, 'Catalog requires --urls-file only')
        targets = [{'canonical_url': l.strip()} for l in args.urls_file.read_text().splitlines()
                   if l.strip() and not l.lstrip().startswith('#')]
    else:
        require(args.input is not None and args.urls_file is None, 'Refresh requires --input only')
        require(args.input.resolve() != args.output.resolve(), 'Refresh output must not replace its input catalog')
        payload = json.loads(args.input.read_text())
        require(payload.get('mode') == 'catalog', 'Refresh input must be a catalog report')
        targets = payload['products']
        require(len({r['source_color_id'] for r in targets}) == len(targets), 'Duplicate source identities')
    require(1 <= len(targets) <= 10, 'Proof supports 1–10 products')
    require(len({r['canonical_url'] for r in targets}) == len(targets), 'Duplicate URLs')
    for row in targets:
        slug_from_url(row['canonical_url'])
    return targets


def run(mode, targets, client):
    results, observations = [], []
    for index, target in enumerate(targets):
        start = len(client.diagnostics)
        entry = {'url': target['canonical_url']}
        try:
            product, inventory = client.fetch(target['canonical_url'])
            observed_at = datetime.now(timezone.utc).isoformat()
            if mode == 'catalog':
                row, flags = catalog_row(product, inventory, target['canonical_url'], observed_at)
            else:
                row, flags = refresh_patch(target, product, inventory, observed_at)
            observations.append(row)
            entry.update(status='partial' if flags else 'success', warnings=flags,
                         source_color_id=product['sku'])
        except Blocked as exc:
            entry.update(status='blocked', error=str(exc))
        except Exception as exc:
            entry.update(status='error', error=f'{type(exc).__name__}: {exc}')
        entry['requests'] = client.diagnostics[start:]
        results.append(entry)
        print(f"[{index + 1}/{len(targets)}] {entry['status']}: {target['canonical_url']}",
              file=sys.stderr, flush=True)
        if entry['status'] == 'blocked':
            results.extend({'url': rest['canonical_url'], 'status': 'skipped',
                            'error': 'Run stopped after a source block'} for rest in targets[index + 1:])
            break
    return {'mode': mode, 'profile': PROFILE, 'completed_at': datetime.now(timezone.utc).isoformat(),
            'requested': len(targets), 'succeeded': sum(r['status'] == 'success' for r in results),
            'partial': sum(r['status'] == 'partial' for r in results),
            'results': results, 'products' if mode == 'catalog' else 'updates': observations}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=['catalog', 'refresh'])
    parser.add_argument('--urls-file', type=Path)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--delay', type=float, default=3)
    parser.add_argument('--timeout', type=float, default=25)
    args = parser.parse_args(argv)
    if not 1 <= args.delay <= 60 or not 1 <= args.timeout <= 120:
        parser.error('Delay must be 1–60 seconds and timeout 1–120 seconds')
    try:
        targets = read_targets(args)
        client = Client(delay=args.delay, timeout=args.timeout)
        try:
            result = run(args.mode, targets, client)
        finally:
            client.close()
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        return int(result['succeeded'] != result['requested'])
    except Exception as exc:
        print(f'Weekday API proof failed ({type(exc).__name__}): {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
