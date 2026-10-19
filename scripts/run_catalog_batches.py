"""Resume weekly non-Kaufmann catalogues, releasing the inventory lock per batch."""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import sync_store_catalogs as sync
from scripts.catalog_manifest import save_state
from scripts import special_store_batches as special

STORES = [*sync.CATALOG_SPECS, *special.SPECS]


def catalog_rows(client, store):
    return (special.read_rows(client, store, include_unavailable=True)
            if store in special.SPECS else client.list_rows(store))

LOG = logging.getLogger(__name__)
LOCK = '/tmp/highlide-inventory-refresh.lock'


class ReconciliationPlan:
    def __init__(self):
        self.patches, self.history = [], []

    def patch_ids(self, table, ids, payload):
        self.patches.extend({'table': table, 'id': row_id, 'payload': payload} for row_id in ids)

    def patch_row(self, table, row_id, payload):
        self.patch_ids(table, [row_id], payload)

    def insert_history(self, rows):
        self.history.extend(rows)


def apply_plan(client, plan):
    # The saved plan makes partial reconciliation replay idempotent. Never replay
    # it over an inventory observation made since the plan was prepared.
    for patch in plan['patches']:
        response = client.session.patch(
            client._table_url(patch['table']),
            params={'id': f"eq.{patch['id']}", 'updated_at': f"lte.{plan['checked_at']}", 'select': 'id'},
            headers={'Prefer': 'return=representation'}, json=patch['payload'],
            timeout=client.timeout_seconds,
        )
        response.raise_for_status()
        if response.json() != [{'id': patch['id']}]:
            raise RuntimeError('Catalog reconciliation journal is stale; manual review required')
    for start in range(0, len(plan['history']), 50):
        client.insert_history(plan['history'][start:start + 50])


def run_child(command, timeout):
    process = subprocess.Popen(command, cwd=ROOT, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except BaseException:
        # Terminate browser descendants too, retaining the lock until they stop.
        import os
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # The importer can exit before its browser descendants do.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        raise


def ensure_run(recorder, state, store, job):
    if job.get('run_id') is not None:
        return
    # Recover a successful start whose response/checkpoint was lost.
    response = recorder.session.get(recorder.table_url,
        params={'batch_id': f"eq.{state['cycle_id']}", 'store': f'eq.{store}', 'select': 'id'},
        timeout=recorder.timeout_seconds)
    response.raise_for_status()
    rows = response.json()
    if len(rows) > 1:
        raise RuntimeError('Duplicate catalog cycle records require review')
    job['run_id'] = int(rows[0]['id']) if rows else recorder.start(
        batch_id=state['cycle_id'], store=store, run_type='weekly_catalog_sync',
        started_at=job['started_at'], rows_before=len(job['before']),
        details={'batch_pages': state['batch_pages'], 'resumable': True})


def process_store(state, store, path, client, recorder, args):
    job = state['jobs'][store]
    if 'before' not in job:
        job.update(before=catalog_rows(client, store), started_at=sync.utc_now(), offset=0, observed={})
        save_state(path, state)
    ensure_run(recorder, state, store, job)
    save_state(path, state)
    manifest = path.parent / (path.name + '.manifests') / state['cycle_id'] / f'{store}.json'
    items = json.loads(manifest.read_text())['items'] if manifest.exists() else None
    if 'plan' not in job and (items is None or job['offset'] < len(items)):
        began = datetime.now(timezone.utc)
        options = SimpleNamespace(log_level='INFO', dry_run=False, no_delay=False, limit=state['batch_pages'])
        command = ([sys.executable, str(ROOT / 'scripts/run_special_catalog_batch.py'),
                    '--store', store, '--limit', str(state['batch_pages'])]
                   if store in special.SPECS else sync.importer_command(sync.CATALOG_SPECS[store], options))
        command += ['--catalog-manifest', str(manifest), '--offset', str(job['offset'])]
        LOG.info('%s catalog batch: offset=%s limit=%s', store, job['offset'], state['batch_pages'])
        result = run_child(command, args.batch_timeout)
        if result:
            raise RuntimeError(f'{store} importer exited {result}; batch retained for retry')
        items = json.loads(manifest.read_text())['items']
        # This read occurs under the same global lock as the import. Daily
        # refreshes between batches cannot masquerade as catalogue observations.
        if store in special.SPECS:
            from scripts.run_special_catalog_batch import report_path
            report = json.loads(report_path(manifest, job['offset']).read_text())
            if not report['done']:
                raise ValueError('Special catalog batch did not commit')
            job['observed'].update(report['observed'])
        else:
            for row in client.list_rows(store):
                if sync.parse_timestamp(row.get('updated_at')) >= began:
                    job['observed'][str(row['id'])] = row['updated_at']
        job['offset'] = min(len(items), job['offset'] + state['batch_pages'])
        job['total'] = len(items)
        job.pop('error', None)
        save_state(path, state)
    if job['offset'] == len(items):
        if store in special.SPECS:
            # These stores use direct observations, not generic catalog-miss
            # tombstones. Kaufmann's sweep already confirmed missing colours;
            # Weekday never treats listing absence as physical unavailability.
            after = catalog_rows(client, store)
            before_ids = {row['id'] for row in job['before']}
            recorder.finish(job['run_id'], status='success', completed_at=sync.utc_now(),
                rows_after=len(after), products_seen=len(job['observed']),
                new_products=len({row['id'] for row in after} - before_ids), error_message=None,
                details={'resumable':True, 'completed_pages':job['offset'], 'total_pages':job['total']})
            job['done'] = True
            save_state(path, state)
            return
        if 'plan' not in job:
            plan = ReconciliationPlan()
            checked_at = sync.utc_now()
            stats = sync.reconcile_catalog(store, job['before'], client.list_rows(store),
                sync.parse_timestamp(job['started_at']), checked_at, plan,
                miss_confirmations=args.miss_confirmations, min_seen_ratio=args.min_seen_ratio,
                seen_product_ids={int(key) for key in job['observed']},
                observed_at={int(key): value for key, value in job['observed'].items()})
            job['plan'] = dict(patches=plan.patches, history=plan.history,
                               checked_at=checked_at, stats=asdict(stats))
            save_state(path, state)
        apply_plan(client, job['plan'])
        stats = job['plan']['stats']
        recorder.finish(job['run_id'], status='safety_blocked' if stats['missing_check_skipped'] else 'success',
            completed_at=job['plan']['checked_at'], rows_after=stats['rows_after'],
            products_seen=stats['seen'], new_products=stats['new'], reactivated_products=stats['reactivated'],
            absent_products=stats['first_misses'] + stats['confirmed_missing'],
            confirmed_missing_products=stats['confirmed_missing'], missing_check_skipped=stats['missing_check_skipped'],
            error_message='Catalog coverage below safety threshold' if stats['missing_check_skipped'] else None,
            details={'resumable': True, 'completed_pages': job['offset'], 'total_pages': job['total']})
        job['done'] = True
        save_state(path, state)
    else:
        recorder.finish(job['run_id'], status='running', error_message=None, completed_at=None,
            products_seen=len(job['observed']), details={'resumable': True,
            'completed_pages': job['offset'], 'total_pages': job['total']})
    LOG.info('%s catalog progress: %s/%s discovery pages; complete=%s',
             store, job['offset'], job['total'], job.get('done', False))


def tick(args):
    path = Path(args.checkpoint).resolve()
    # Separate coordinator lock prevents two invocations from racing while the
    # inventory lock is deliberately released between stores.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + '.lock').open('a') as coordinator:
        try:
            fcntl.flock(coordinator, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        state = json.loads(path.read_text()) if path.exists() else None
        settings = dict(batch_pages=args.batch_pages, miss_confirmations=args.miss_confirmations,
                        min_seen_ratio=args.min_seen_ratio)
        if state and (state.get('version') != 2 or state['stores'] != args.store or state['settings'] != settings):
            raise ValueError('Checkpoint configuration mismatch; do not discard unfinished work')
        if state and all(job.get('done') for job in state['jobs'].values()):
            if time.time() < state['next_cycle_at']:
                return 0
            state = None
        if state is None:
            state = dict(version=2, cycle_id=str(uuid.uuid4()), stores=args.store, batch_pages=args.batch_pages,
                         settings=settings, next_store=0,
                         next_cycle_at=time.time() + 7 * 86400, jobs={store: {} for store in args.store})
            save_state(path, state)
        url = sync.env('SUPABASE_URL')
        key = sync.env('SUPABASE_SECRET_KEY') or sync.env('SUPABASE_SERVICE_ROLE_KEY')
        if not url or not key:
            raise ValueError('Supabase server credentials required')
        client = sync.CatalogLifecycleClient(url, key, history_table=sync.env('STORE_INVENTORY_HISTORY_TABLE', 'store_inventory_history'))
        recorder = sync.CatalogSyncRunRecorder(url, key, table=sync.env('CATALOG_SYNC_RUNS_TABLE', 'catalog_sync_runs'))
        failures = 0
        try:
            start = state['next_store']
            for index in [(start + offset) % len(state['stores']) for offset in range(len(state['stores']))]:
                store = state['stores'][index]
                job = state['jobs'][store]
                if job.get('done') or job.get('retry_at', 0) > time.time():
                    continue
                with open(LOCK, 'a') as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        LOG.info('Inventory/catalog job holds the lock; resume on next tick')
                        break
                    try:
                        process_store(state, store, path, client, recorder, args)
                    except Exception as exc:
                        failures += 1
                        LOG.exception('%s batch retained for retry', store)
                        # Subprocess exit codes do not distinguish rate limits;
                        # conservatively defer failed sources for 24 hours.
                        job.update(error=str(exc)[:2000], retry_at=time.time() + 86400)
                        save_state(path, state)
                        if job.get('run_id'):
                            recorder.finish(job['run_id'], status='partial', completed_at=None,
                                error_message=job['error'], details={'resumable': True,
                                'completed_pages': job.get('offset', 0), 'retry_at': job['retry_at']})
                    state['next_store'] = (index + 1) % len(state['stores'])
                    save_state(path, state)
                time.sleep(5)
        finally:
            recorder.close()
            client.close()
        return 1 if failures else 0


def parse_args():
    sync.load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', action='append', choices=STORES)
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--batch-pages', type=int, default=10)
    parser.add_argument('--batch-timeout', type=int, default=3600)
    parser.add_argument('--miss-confirmations', type=int, default=2)
    parser.add_argument('--min-seen-ratio', type=float, default=0.5)
    args = parser.parse_args()
    args.store = list(dict.fromkeys(args.store or STORES))
    if args.batch_pages < 1 or args.batch_timeout < 1 or args.miss_confirmations < 2 or not 0 < args.min_seen_ratio <= 1:
        parser.error('Invalid batch size, timeout or catalog safety threshold')
    return args


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    # systemd stop must unwind run_child before releasing the global lock.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    raise SystemExit(tick(parse_args()))
