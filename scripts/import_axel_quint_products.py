"""Bounded colour-row catalogue imports. Dry runs never create a DB client."""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
import uuid
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrapers.full_import.axel_quint import AxelQuintScraper, SourceUnavailable
from scrapers.source_http import SourcePayloadError, StoreRateLimited
from scripts.import_rains_products import SupabaseCatalogClient, env, load_dotenv

from scripts.catalog_manifest import manifest_items, add_manifest_argument

LOG = logging.getLogger(__name__)


def save_checkpoint(path, state):
    """Atomically record only pages whose rows have been acknowledged by the DB."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    temporary.replace(path)


def committed_salling_pages(log_path):
    """Recover conservative progress from the pre-checkpoint importer log.

    The old writer could split a colour family across batches, so keep the
    most recently hydrated family eligible for replay at every write marker.
    """
    current = None
    hydrated, committed = [], set()
    for line in Path(log_path).read_text().splitlines():
        if 'Running weekly full import for salling:' in line:
            current, hydrated, committed = None, [], set()
        if ' INFO salling [' in line and '] Hydrating ' in line:
            current = line.split('] Hydrating ', 1)[1].strip()
        elif ' INFO salling hydrated ' in line and current:
            hydrated.append(current)
            current = None
        elif ' INFO Upserted ' in line and ' salling colour rows.' in line:
            committed.update(hydrated[:-1])
    return committed


def import_products(store, args, scraper_factory=None):
    availability_first = getattr(args, 'availability_first', False)
    if availability_first and store not in {'axel', 'quint'}:
        raise ValueError('Availability-first import supports only AXEL/qUINT')
    scraper_factory = scraper_factory or AxelQuintScraper
    scraper_options = dict(max_retries=args.max_retries)
    if store in {'salling', 'magasin'}:
        scraper_options['request_delay'] = getattr(args, 'request_delay', 3)
    scraper = scraper_factory(store, **scraper_options)
    client = None
    lifecycle = None
    existing_by_key = {}
    rows, pending, keys = [], [], set()
    failed = 0
    checkpoint_path = getattr(args, 'checkpoint', None)
    checkpoint = None
    completed = set()
    acknowledged_pages = set()
    flush = None
    deferred = set()
    blocked = False
    consecutive_source_errors = 0
    try:
        if checkpoint_path and Path(checkpoint_path).exists():
            checkpoint = json.loads(Path(checkpoint_path).read_text())
            if checkpoint.get('version') != 1 or checkpoint.get('store') != store:
                raise ValueError('Checkpoint version/store mismatch')
            urls = checkpoint['urls']
            if not isinstance(urls, list) or not urls or any(not isinstance(u, str) or scraper.clean_url(u) != u for u in urls):
                raise ValueError('Invalid checkpoint URLs')
            completed = set(checkpoint['completed'])
            if not completed.issubset(urls):
                raise ValueError('Checkpoint completed pages are outside its manifest')
        else:
            urls = sorted({scraper.clean_url(url) for url in args.url}) if args.url else manifest_items(args, store, lambda: scraper.discover_product_urls())
            if checkpoint_path:
                if getattr(args, 'seed_import_log', None):
                    completed = committed_salling_pages(args.seed_import_log).intersection(urls)
                    LOG.info('Recovered %s committed Salling source pages from the previous log.', len(completed))
                checkpoint = dict(version=1, store=store, urls=urls, completed=[])
                checkpoint['completed'] = sorted(completed)
                save_checkpoint(checkpoint_path, checkpoint)
        manifest_count = len(urls)
        manifest_urls = set(urls)
        if checkpoint is not None:
            checkpoint.setdefault('failures', {})
            deferred = set(checkpoint.get('deferred', checkpoint['failures'])) - completed
            if getattr(args, 'retry_failed', False):
                deferred.clear()
            checkpoint['deferred'] = sorted(deferred)
            checkpoint['last_batch'] = dict(id=str(uuid.uuid4()), status='running')
            save_checkpoint(checkpoint_path, checkpoint)
        urls = [u for u in urls if u not in completed and u not in deferred]
        if checkpoint is not None:
            LOG.info('%s resume: saved_pages=%s remaining_pages=%s deferred_errors=%s', store, len(completed), len(urls), len(deferred))
        urls = urls[args.offset:]
        if args.limit:
            urls = urls[:args.limit]
        if getattr(args, 'batch_pages', None):
            urls = urls[:args.batch_pages]
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
            if availability_first:
                from scripts.sync_store_catalogs import CatalogLifecycleClient
                lifecycle = CatalogLifecycleClient(url, key, history_table='store_inventory_history')
                existing_by_key = {(r['source_parent_id'], r['source_color_id']): r
                                   for r in lifecycle.list_rows(store)}

        def flush():
            if pending:
                client.upsert_products(f'{store}_products', list(pending))
                LOG.info('Upserted %s %s colour rows.', len(pending), store)
                pending.clear()
            if checkpoint is not None:
                completed.update(acknowledged_pages)
                deferred.difference_update(completed)
                checkpoint['completed'] = sorted(completed)
                checkpoint['deferred'] = sorted(deferred)
                for done_url in completed:
                    checkpoint['failures'].pop(done_url, None)
                save_checkpoint(checkpoint_path, checkpoint)
                acknowledged_pages.clear()
                LOG.info('Saved %s checkpoint: completed=%s remaining=%s', store,
                         len(completed), manifest_count - len(completed))

        skipped_unavailable = updated_unavailable = 0
        for index, url in enumerate(urls, 1):
            covered = getattr(scraper, 'covered_urls', set())
            if isinstance(covered, set) and url in covered:
                acknowledged_pages.add(url)
                continue
            if index > 1 and not args.no_delay:
                time.sleep(random.uniform(args.min_delay, args.max_delay))
            LOG.info('%s [%s/%s] Hydrating %s', store, index, len(urls), url)
            covered_before = set(covered) if isinstance(covered, set) else None
            try:
                started = time.monotonic()
                snapshot = (scraper.fetch_catalog_snapshot(url) if availability_first
                            else scraper.fetch_snapshot(url))
                batch = scraper.rows_from_snapshot(snapshot)
                LOG.info('%s hydrated %s colour rows in %.2fs (%s unavailable).', store, len(batch), time.monotonic() - started, sum(r.get("publication_status") == "unavailable" for r in batch))
                if args.fixtures_dir:
                    directory = Path(args.fixtures_dir)
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / f'{store}-{index}.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
            except StoreRateLimited as exc:
                if covered_before is not None:
                    scraper.covered_urls = covered_before
                failed += 1
                blocked = True
                if checkpoint is not None:
                    checkpoint['last_batch']['error'] = str(exc)
                LOG.warning('Stopping %s import: %s; catalogue reconciliation must be skipped.', store, exc)
                break
            except Exception as exc:
                if covered_before is not None:
                    scraper.covered_urls = covered_before
                failed += 1
                response = getattr(exc, 'response', None)
                server_error = (isinstance(exc, requests.HTTPError) and response is not None and
                                500 <= response.status_code < 600)
                recoverable_source_error = server_error or isinstance(exc, SourcePayloadError)
                consecutive_source_errors = consecutive_source_errors + 1 if recoverable_source_error else 0
                transport_failure = isinstance(exc, requests.RequestException) and not isinstance(exc, SourceUnavailable)
                if ((transport_failure and not recoverable_source_error) or
                        (recoverable_source_error and consecutive_source_errors >= 25)):
                    blocked = True
                    if checkpoint is not None:
                        checkpoint['last_batch']['error'] = str(exc)[:2000]
                    LOG.exception('Stopping %s import after a source failure (%s consecutive server/payload errors); saved rows retained.', store, consecutive_source_errors)
                    break
                if checkpoint is not None:
                    previous = checkpoint['failures'].get(url, {})
                    checkpoint['failures'][url] = dict(error=str(exc)[:2000], error_type=type(exc).__name__,
                                                     attempts=previous.get('attempts', 0) + 1)
                    deferred.add(url)
                LOG.exception('Failed %s product %s; catalogue reconciliation must be skipped.', store, url)
                continue
            consecutive_source_errors = 0
            family_rows = []
            for row in batch:
                identity = (row['source_parent_id'], row['source_color_id'])
                if identity in keys:
                    continue
                keys.add(identity)
                if args.dry_run:
                    rows.append(row)
                elif availability_first and row['publication_status'] == 'unavailable':
                    existing = existing_by_key.get(identity)
                    if existing:
                        from scripts.refresh_store_inventory import STORE_SPECS, dynamic_payload
                        payload = dynamic_payload(STORE_SPECS[store], row)
                        payload.update({key: row[key] for key in ('publication_status', 'status_reason', 'status_checked_at')})
                        payload['discontinued_at'] = existing.get('discontinued_at') or row['discontinued_at']
                        # Mark seen via updated_at without rewriting stable metadata/raw.
                        lifecycle.patch_row(f'{store}_products', int(existing['id']), payload)
                        updated_unavailable += 1
                    else:
                        skipped_unavailable += 1
                else:
                    family_rows.append(row)
            # Only stage a fully validated colour family. An interrupt during
            # hydration or row preparation can flush earlier complete families.
            pending.extend(family_rows)
            # A colour family is committed together before marking its URL done.
            acknowledged_pages.add(url)
            if checkpoint is not None and isinstance(getattr(scraper, 'covered_urls', None), set):
                # fetch_snapshot validates the whole family; persist related
                # colour URLs too, including those outside this 100-page batch.
                acknowledged_pages.update(scraper.covered_urls.intersection(manifest_urls))
            if len(pending) >= args.write_batch_size:
                flush()
        if checkpoint is not None:
            checkpoint['last_batch']['status'] = 'blocked' if blocked else 'partial' if failed else 'success'
            checkpoint['last_batch']['failed_pages'] = failed
        flush()
        if checkpoint is not None:
            LOG.info('%s checkpoint: completed=%s remaining=%s total=%s', store,
                     len(completed), manifest_count - len(completed), manifest_count)
        if args.dry_run:
            rendered = json.dumps(rows, ensure_ascii=False, indent=2)
            if args.output:
                Path(args.output).write_text(rendered)
            else:
                print(rendered)
        if availability_first:
            LOG.info('%s HTTP-screened unavailable colours: existing_updated=%s new_skipped=%s', store, updated_unavailable, skipped_unavailable)
        LOG.info('%s import %s. rows=%s failed_pages=%s', store, 'stopped' if blocked else 'finished', len(keys), failed)
        return 1 if failed else 0
    except KeyboardInterrupt:
        LOG.info('Stopping %s import; saving fully parsed pending families.', store)
        if flush is not None and client is not None:
            try:
                flush()
            except Exception:
                LOG.exception('Could not save pending rows; last committed checkpoint retained.')
                return 1
        LOG.info('%s stopped. Rerun with the same checkpoint to resume.', store)
        return 130
    except Exception:
        LOG.exception('%s import failed', store)
        return 1
    finally:
        scraper.close()
        if client:
            client.session.close()
        if lifecycle:
            lifecycle.close()


def parse_args(store, argv=None):
    parser = argparse.ArgumentParser(description=f'Import {store} clothing/footwear and published shop inventory.')
    add_manifest_argument(parser)
    parser.add_argument('--url', action='append', default=[])
    parser.add_argument('--limit', type=int)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--checkpoint', help='Persistent discovery manifest and committed pages; rerun to resume. Use a separate file for each catalogue run.')
    parser.add_argument('--batch-pages', type=int, help='Maximum remaining source pages in this invocation; requires --checkpoint.')
    parser.add_argument('--seed-import-log', help='Salling only: recover committed pages from the old import log when creating a new checkpoint.')
    parser.add_argument('--retry-failed', action='store_true', help='Include previously deferred pages in this checkpoint invocation.')
    if store in {'salling', 'magasin'}:
        parser.add_argument('--request-delay', type=float, default=3, help='Minimum seconds between source requests (default: 3; independent of --no-delay).')
    parser.add_argument('--discover-only', action='store_true')
    if store in {'axel', 'quint'}:
        parser.add_argument('--availability-first', action='store_true', help='HTTP availability check before browser metadata extraction; update known unavailable colours without importing new unavailable colours.')
    parser.add_argument('--preview', type=int, default=10)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--output', help='Save dry-run rows or discovery URLs locally.')
    parser.add_argument('--fixtures-dir', help='Save public snapshots locally for fixture tests (dry run only).')
    parser.add_argument('--write-batch-size', type=int, default=10 if store in {'salling', 'magasin'} else 50)
    parser.add_argument('--min-delay', type=float, default=1.5)
    parser.add_argument('--max-delay', type=float, default=3)
    parser.add_argument('--no-delay', action='store_true')
    parser.add_argument('--max-retries', type=int, default=2)
    parser.add_argument('--log-level', default='INFO', choices=['DEBUG','INFO','WARNING','ERROR'])
    args = parser.parse_args(argv)
    if args.checkpoint and (args.dry_run or args.discover_only or args.url or args.offset or args.limit or getattr(args, 'availability_first', False)):
        parser.error('--checkpoint requires an unrestricted real import without availability-first')
    if args.batch_pages is not None and (args.batch_pages < 1 or not args.checkpoint):
        parser.error('--batch-pages must be positive and requires --checkpoint')
    if args.seed_import_log and (store != 'salling' or not args.checkpoint):
        parser.error('--seed-import-log requires a Salling checkpoint import')
    if args.retry_failed and not args.checkpoint:
        parser.error('--retry-failed requires --checkpoint')
    if hasattr(args, 'request_delay') and (not 0.5 <= args.request_delay <= 60):
        parser.error('--request-delay must be between 0.5 and 60 seconds')
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
