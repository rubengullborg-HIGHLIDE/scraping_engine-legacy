"""Bounded colour-row catalogue imports. Dry runs never create a DB client."""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrapers.full_import.axel_quint import AxelQuintScraper
from scripts.import_rains_products import SupabaseCatalogClient, env, load_dotenv

LOG = logging.getLogger(__name__)


def import_products(store, args, scraper_factory=None):
    scraper_factory = scraper_factory or AxelQuintScraper
    scraper = scraper_factory(store, max_retries=args.max_retries)
    client = None
    rows, pending, keys = [], [], set()
    failed = 0
    try:
        urls = sorted({scraper.clean_url(url) for url in args.url}) if args.url else scraper.discover_product_urls()
        urls = urls[args.offset:]
        if args.limit:
            urls = urls[:args.limit]
        LOG.info('Selected %s %s source pages (clothing/footwear classification occurs during hydration).', len(urls), store)
        if args.discover_only:
            for url in urls[:args.preview]:
                LOG.info('Discovered %s', url)
            if args.output:
                Path(args.output).write_text(json.dumps(urls, indent=2))
            return 0
        if not args.dry_run:
            load_dotenv(ROOT / '.env')
            url, key = env('SUPABASE_URL'), env('SUPABASE_SECRET_KEY') or env('SUPABASE_SERVICE_ROLE_KEY')
            if not url or not key:
                raise ValueError('Supabase credentials required for writes')
            client = SupabaseCatalogClient(url, key)
        for index, url in enumerate(urls, 1):
            covered = getattr(scraper, 'covered_urls', set())
            if isinstance(covered, set) and url in covered:
                continue
            if index > 1 and not args.no_delay:
                time.sleep(random.uniform(args.min_delay, args.max_delay))
            LOG.info('%s [%s/%s] Hydrating %s', store, index, len(urls), url)
            try:
                started = time.monotonic()
                snapshot = scraper.fetch_snapshot(url)
                batch = scraper.rows_from_snapshot(snapshot)
                LOG.info('%s hydrated %s colour rows in %.2fs (%s unavailable).', store, len(batch), time.monotonic() - started, sum(r.get("publication_status") == "unavailable" for r in batch))
                if args.fixtures_dir:
                    directory = Path(args.fixtures_dir)
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / f'{store}-{index}.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
            except Exception:
                failed += 1
                LOG.exception('Failed %s product %s; catalogue reconciliation must be skipped.', store, url)
                continue
            for row in batch:
                identity = (row['source_parent_id'], row['source_color_id'])
                if identity in keys:
                    continue
                keys.add(identity)
                if args.dry_run:
                    rows.append(row)
                else:
                    pending.append(row)
                    if len(pending) >= args.write_batch_size:
                        client.upsert_products(f'{store}_products', pending)
                        LOG.info('Upserted %s %s colour rows.', len(pending), store)
                        pending.clear()
        if pending:
            client.upsert_products(f'{store}_products', pending)
            LOG.info('Upserted final %s %s colour rows.', len(pending), store)
        if args.dry_run:
            rendered = json.dumps(rows, ensure_ascii=False, indent=2)
            if args.output:
                Path(args.output).write_text(rendered)
            else:
                print(rendered)
        LOG.info('%s import complete. rows=%s failed_pages=%s', store, len(keys), failed)
        return 1 if failed else 0
    except Exception:
        LOG.exception('%s import failed', store)
        return 1
    finally:
        scraper.close()
        if client:
            client.session.close()


def parse_args(store, argv=None):
    parser = argparse.ArgumentParser(description=f'Import {store} clothing/footwear and published shop inventory.')
    parser.add_argument('--url', action='append', default=[])
    parser.add_argument('--limit', type=int)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--discover-only', action='store_true')
    parser.add_argument('--preview', type=int, default=10)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--output', help='Save dry-run rows or discovery URLs locally.')
    parser.add_argument('--fixtures-dir', help='Save public snapshots locally for fixture tests (dry run only).')
    parser.add_argument('--write-batch-size', type=int, default=50)
    parser.add_argument('--min-delay', type=float, default=1.5)
    parser.add_argument('--max-delay', type=float, default=3)
    parser.add_argument('--no-delay', action='store_true')
    parser.add_argument('--max-retries', type=int, default=2)
    parser.add_argument('--log-level', default='INFO', choices=['DEBUG','INFO','WARNING','ERROR'])
    args = parser.parse_args(argv)
    if args.offset < 0 or (args.limit is not None and args.limit < 1) or args.write_batch_size < 1 or args.max_retries < 0:
        parser.error('Invalid offset, limit, batch size or retries')
    if args.min_delay < 0 or args.max_delay < args.min_delay:
        parser.error('Require 0 <= min-delay <= max-delay')
    if args.fixtures_dir and not args.dry_run:
        parser.error('--fixtures-dir requires --dry-run')
    if args.output and not (args.dry_run or args.discover_only):
        parser.error('--output requires --dry-run or --discover-only')
    return args


def main(store):
    args = parse_args(store)
    logging.basicConfig(level=args.log_level, format='%(asctime)s %(levelname)s %(message)s')
    return import_products(store, args)
