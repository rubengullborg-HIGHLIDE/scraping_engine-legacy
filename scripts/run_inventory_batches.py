"""Single-host, resumable refresh cycles for the shared store adapters.

The checkpoint is a write-ahead journal, not a disposable log. Use this runner
as the sole inventory writer for its selected stores while a cycle is pending.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import logging
import os
from pathlib import Path
import sys
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import refresh_store_inventory as refresh
from scripts import special_store_batches as special

SPECS = {**refresh.STORE_SPECS, **special.SPECS}

LOG = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as output:
        os.chmod(temporary, 0o600)
        json.dump(state, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def batches(store, rows, size):
    # Never split a style/page across batches: disappearance guards need siblings.
    groups = OrderedDict()
    for row in rows:
        key = row['source_parent_id'] if store in ('salling', 'magasin') else refresh.row_url(row)
        groups.setdefault(key, []).append(row)
    values = list(groups.values())
    return [sum(values[index:index + size], []) for index in range(0, len(values), size)]


class StagedClient:
    def __init__(self, store, rows):
        self.store, self.rows = store, rows
        self.patches, self.history = [], []

    def list_rows(self, spec):
        if spec.key != self.store:
            raise ValueError('Adapter attempted to read a different store')
        return copy.deepcopy(self.rows)

    def patch_row(self, spec, row_id, payload):
        if spec.key != self.store or row_id not in {row['id'] for row in self.rows}:
            raise ValueError('Adapter attempted to update outside its batch')
        self.patches.append({'id': row_id, 'payload': copy.deepcopy(payload)})

    def queue_history_observation(self, payload):
        self.history.append(copy.deepcopy(payload))


def put_run(database, payload):
    # Upsert by the saved UUID makes a lost HTTP response safe to retry.
    response = database.session.post(
        database._table_url(database.runs_table),
        headers={'Prefer': 'resolution=merge-duplicates,return=minimal'},
        json=payload, timeout=database.timeout_seconds,
    )
    response.raise_for_status()


class BatchInventoryClient(refresh.SupabaseInventoryClient):
    def list_rows(self, spec):
        if spec.key in special.SPECS:
            return special.read_rows(self, spec.key)
        return super().list_rows(spec)

    def patch_row(self, spec, row_id, payload):
        # Refuse stale journal replay if another writer ran after interruption.
        guard = {'updated_at': f"lte.{payload['updated_at']}"} if payload.get('updated_at') else {}
        if not guard:
            raise ValueError('Observation timestamp required for journal replay')
        response = self.session.patch(
            self._table_url(spec.table),
            params={'id': f'eq.{row_id}', **guard, 'select': 'id'},
            headers={'Prefer': 'return=representation'}, json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        if response.json() != [{'id': row_id}]:
            raise refresh.SupabasePatchError(
                f'{spec.key} id={row_id} is missing or newer than the journal; refusing stale replay')


COUNTERS = ('pages_processed', 'updated_products', 'unavailable_products',
            'history_observations', 'failed_products', 'skipped_products')


def cycle_record(state):
    pending = any(state['jobs'].values())
    failed = state.get('write_failed', False) or any(
        batch['error'] or batch['result']['failed_products'] for batch in state['batches'])
    return {'id': state['cycle_id'], 'mode': 'selected', 'selected_stores': state['stores'],
            'status': 'running' if pending else 'partial' if failed else 'succeeded',
            'started_at': state['started_at'], 'finished_at': state.get('finished_at'),
            'updated_at': now(), 'row_limit': None, 'row_offset': 0, 'history_enabled': True,
            'stores_planned': len(state['stores']),
            'stores_completed': sum(not jobs for jobs in state['jobs'].values()),
            'failed_stores': len({batch['store'] for batch in state['batches']
                                  if batch['error'] or batch['result']['failed_products']}),
            'write_failed': state.get('write_failed', False), **state['totals']}


def commit(database, path, state):
    journal = state['journal']
    store = journal['store']
    for patch in journal['patches']:
        database.patch_row(SPECS[store], patch['id'], patch['payload'])
    # Replay the exact observations, including their original UTC timestamps.
    # The existing history trigger merges repeated identical observations.
    for observation in journal['history']:
        database.queue_history_observation(observation)
    database.flush_history()
    # Build the next checkpoint separately; a failed run write must not advance it.
    next_state = copy.deepcopy(state)
    job = next_state['jobs'][store].pop(0)
    completed = special.completed_ids(store, journal['patches'])
    remaining = [row for row in job['rows'] if row['id'] not in completed]
    if remaining:
        job.update(rows=remaining, retry_at=max(job['retry_at'], time.time() + state['retry_seconds']),
                   error=journal['error'])
        next_state['jobs'][store].append(job)
    next_state['next_store'] = (state['stores'].index(store) + 1) % len(state['stores'])
    next_state['journal'] = None
    next_state['active_batch'] = None
    for counter in COUNTERS:
        next_state['totals'][counter] += journal['result'][counter]
    next_state['batches'].append({'id': job['id'], 'store': store, 'started_at': journal['started_at'],
                                 'finished_at': now(), 'result': journal['result'], 'error': journal['error']})
    if not any(next_state['jobs'].values()):
        next_state['finished_at'] = now()
    put_run(database, cycle_record(next_state))
    save(path, next_state)
    state.clear()
    state.update(next_state)


def run_cycle(database, path, state, args, max_batches):
    try:
        return process_cycle(database, path, state, args, max_batches)
    except (refresh.SupabasePatchError, refresh.requests.RequestException):
        state['write_failed'] = True
        save(path, state)
        try:
            put_run(database, {**cycle_record(state), 'status': 'failed'})
        except Exception:
            LOG.exception('Could not record database failure for cycle %s', state['cycle_id'])
        raise


def process_cycle(database, path, state, args, max_batches):
    if state['journal']:
        commit(database, path, state)
    if not any(state['jobs'].values()) and not state.get('finished_at'):
        state['finished_at'] = now()
        save(path, state)
    put_run(database, cycle_record(state))
    attempted = set()
    count = 0
    while not max_batches or count < max_batches:
        ordered = state['stores'][state['next_store']:] + state['stores'][:state['next_store']]
        store = next((key for key in ordered if any(job['id'] not in attempted
                      and job['retry_at'] <= time.time() for job in state['jobs'][key])), None)
        if store is None:
            break
        index = next(index for index, job in enumerate(state['jobs'][store])
                     if job['id'] not in attempted and job['retry_at'] <= time.time())
        state['jobs'][store].insert(0, state['jobs'][store].pop(index))
        job = state['jobs'][store][0]
        attempted.add(job['id'])
        started = now()
        if state.get('active_batch'):
            LOG.warning('Resuming interrupted batch %s in cycle %s', state['active_batch']['id'], state['cycle_id'])
        state['active_batch'] = {'id': job['id'], 'store': store, 'started_at': started}
        save(path, state)
        put_run(database, cycle_record(state))
        staged = StagedClient(store, job['rows'])
        error = None
        LOG.info('Starting %s batch %s: %s rows', store, job['id'], len(job['rows']))
        try:
            stats = (special.run(staged, args) if store in special.SPECS
                     else refresh.STORE_REFRESHERS[store](staged, args))
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            LOG.exception('%s batch failed; retaining uncommitted rows for retry', store)
            stats = getattr(exc, 'stats', None) or refresh.RefreshStats(failed=len(job['rows']))
            # An adapter-level guard may invalidate its entire staged sample.
            staged.patches.clear()
            staged.history.clear()
            if isinstance(exc, refresh.StoreRateLimited):
                cooldown = max(state['retry_seconds'], exc.retry_after_seconds or 86400)
                for pending in state['jobs'][store]:
                    pending['retry_at'] = time.time() + cooldown
        patch_ids = [patch['id'] for patch in staged.patches]
        history_ids = [entry['product_id'] for entry in staged.history]
        valid_history = (set(history_ids).issubset(patch_ids) if store in special.SPECS
                         else sorted(patch_ids) == sorted(history_ids))
        if (len(set(patch_ids)) != len(patch_ids) or len(set(history_ids)) != len(history_ids) or not valid_history
                or any(entry.get('store') != store for entry in staged.history)):
            raise ValueError('Each committed row must have exactly one history observation')
        incomplete = len(special.completed_ids(store, staged.patches)) < len(job['rows'])
        result = {'status': 'partial' if error or stats.failed or stats.skipped or incomplete else 'succeeded',
                  'stores_completed': 0 if error else 1, 'failed_stores': int(error is not None),
                  'pages_processed': stats.pages, 'updated_products': len(staged.patches) - stats.unavailable if not error else 0,
                  'unavailable_products': stats.unavailable if not error else 0,
                  'history_observations': len(staged.history), 'failed_products': stats.failed,
                  'skipped_products': stats.skipped, 'write_failed': False}
        state['journal'] = {'store': store, 'started_at': started, 'result': result,
                            'patches': staged.patches, 'history': staged.history,
                            'error': error or ('Rows were not committed; see batch logs' if incomplete else None)}
        save(path, state)
        commit(database, path, state)
        LOG.info('%s batch finished: %s', store, asdict(stats))
        count += 1
    return int(any(state['jobs'].values()))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', action='append', choices=list(SPECS), help='Repeat to select stores; defaults to all 14.')
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--batch-pages', type=int, default=20)
    parser.add_argument('--max-batches', type=int, default=0, help='Stop after this many batches; 0 runs all due work.')
    parser.add_argument('--retry-seconds', type=int, default=3600)
    parser.add_argument('--new-cycle', action='store_true', help='Start a fresh snapshot only after the previous cycle completed.')
    parser.add_argument('--scheduled', action='store_true', help='Resume one batch; start a new cycle when complete and 24 hours old.')
    parser.add_argument('--source-transport', choices=('requests', 'system-curl'), default='system-curl')
    options = parser.parse_args(argv)
    if options.batch_pages < 1 or options.max_batches < 0 or options.retry_seconds < 1:
        parser.error('Batch size and retry interval must be positive; max-batches must be nonnegative')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    args = refresh.parse_args(['--source-transport', options.source_transport])
    url = refresh.env('SUPABASE_URL')
    key = refresh.env('SUPABASE_SECRET_KEY') or refresh.env('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise RuntimeError('SUPABASE_URL and Supabase secret credentials are required')
    stores = list(dict.fromkeys(options.store or SPECS))
    with open('/tmp/highlide-inventory-refresh.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if options.scheduled:
                return 0
            raise
        database = BatchInventoryClient(url, key, history_table=args.history_table,
            runs_table=args.runs_table, timeout_seconds=args.supabase_timeout, max_retries=args.max_retries)
        try:
            path = options.checkpoint.resolve()
            state = json.loads(path.read_text()) if path.exists() else None
            identity = {'version': 3, 'url': url.rstrip('/'), 'stores': stores,
                        'history_table': args.history_table, 'runs_table': args.runs_table,
                        'batch_pages': options.batch_pages, 'retry_seconds': options.retry_seconds,
                        'source_transport': options.source_transport}
            if state and any(state.get(key) != value for key, value in identity.items()):
                raise ValueError('Checkpoint configuration differs; resume with its original configuration')
            if options.scheduled and state and not state['journal'] and not any(state['jobs'].values()):
                if time.time() < datetime.fromisoformat(state['started_at']).timestamp() + 86400:
                    return 0
                state = None
            if state and options.new_cycle:
                if state['journal'] or any(state['jobs'].values()):
                    raise ValueError('Cannot replace an unfinished cycle')
                state = None
            if state is None:
                state = {**identity, 'next_store': 0, 'journal': None, 'jobs': {},
                         'cycle_id': str(uuid.uuid4()), 'started_at': now(),
                         'totals': dict.fromkeys(COUNTERS, 0), 'batches': [], 'active_batch': None}
                for store in stores:
                    rows = database.list_rows(SPECS[store])
                    state['jobs'][store] = [{'id': str(uuid.uuid4()), 'rows': batch, 'retry_at': 0}
                                            for batch in batches(store, rows, options.batch_pages)]
                save(path, state)
            return_code = run_cycle(database, path, state, args, 1 if options.scheduled else options.max_batches)
            return 0 if options.scheduled else return_code
        finally:
            database.close()


if __name__ == '__main__':
    raise SystemExit(main())
