"""Resume one initial department-store import in bounded, locked batches.

This does not perform weekly lifecycle reconciliation: partial imports must
never mark unseen catalogue products missing.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger(__name__)


def batch_can_continue(before, after, returncode):
    """Only a fresh, fully processed batch with isolated page errors may continue."""
    batch = after.get('last_batch', {})
    fresh = batch.get('id') != before.get('last_batch', {}).get('id')
    progress = (set(after.get('completed', [])) != set(before.get('completed', [])) or
                set(after.get('deferred', [])) != set(before.get('deferred', [])))
    return fresh and progress and batch.get('status') in ('success', 'partial') and returncode in (0, 1)


def run_batch(command, timeout):
    # Isolate the child so Ctrl+C reaches it once, via the parent. Hold the
    # shared lock until its final pending write and checkpoint have finished.
    process = subprocess.Popen(command, cwd=ROOT, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except KeyboardInterrupt:
        LOG.info('Stopping batch; waiting for the importer to save pending rows. Press Ctrl+C only once.')
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=90)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            process.kill()
            process.wait()
            LOG.warning('Importer forcibly stopped; resume from its last saved checkpoint.')
        return 130
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True, choices=('salling', 'magasin'))
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--batch-pages', type=int, default=100)
    parser.add_argument('--write-batch-size', type=int, default=10)
    parser.add_argument('--batch-timeout', type=int, default=10800)
    parser.add_argument('--request-delay', type=float, default=3, help='Minimum seconds between source requests (default: 3).')
    parser.add_argument('--lock', default='/tmp/highlide-inventory-refresh.lock')
    parser.add_argument('--seed-import-log', help='Salling only: seed a new checkpoint from confirmed writes in the previous log.')
    parser.add_argument('--retry-failed', action='store_true', help='Make previously deferred errors eligible once at startup.')
    args = parser.parse_args()
    if args.batch_pages < 1 or args.batch_timeout < 1 or args.write_batch_size < 1:
        parser.error('Batch size and timeout must be positive')
    if not 0.5 <= args.request_delay <= 60:
        parser.error('--request-delay must be between 0.5 and 60 seconds')
    if args.seed_import_log and args.store != 'salling':
        parser.error('--seed-import-log is supported for Salling only')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    checkpoint = Path(args.checkpoint).resolve()
    command = [sys.executable, '-u', str(ROOT / f'scripts/import_{args.store}_products.py'),
               '--checkpoint', str(checkpoint), '--batch-pages', str(args.batch_pages),
               '--write-batch-size', str(args.write_batch_size),
               '--request-delay', str(args.request_delay)]
    if args.seed_import_log:
        command.extend(['--seed-import-log', str(Path(args.seed_import_log).resolve())])
    while True:
        with open(args.lock, 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                LOG.info('Waiting for existing inventory/catalogue job to release %s', args.lock)
                time.sleep(30)
                continue
            # Read completion under the same lock used for writes.
            state = {}
            if checkpoint.exists():
                state = json.loads(checkpoint.read_text())
                if state.get('store') != args.store or state.get('version') != 1:
                    raise ValueError('Checkpoint version/store mismatch')
                if args.retry_failed:
                    state['deferred'] = []
                    temporary = checkpoint.with_name(checkpoint.name + '.tmp')
                    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2))
                    temporary.replace(checkpoint)
                    args.retry_failed = False
                done = set(state['completed'])
                deferred = set(state.get('deferred', state.get('failures', {}))) - done
                if state['urls'] and set(state['urls']).issubset(done | deferred):
                    if deferred:
                        LOG.warning('%s catalogue pass finished: saved=%s deferred_errors=%s. Error details are in %s; use --retry-failed after fixing them.', args.store, len(done), len(deferred), checkpoint)
                        return 1
                    LOG.info('%s initial import complete: %s source pages', args.store, len(state['urls']))
                    return 0
            LOG.info('Starting %s batch of at most %s pages', args.store, args.batch_pages)
            try:
                returncode = run_batch(command, args.batch_timeout)
            except subprocess.TimeoutExpired:
                LOG.error('Batch timed out; committed pages retained in %s. Rerun to resume.', checkpoint)
                return 1
            if returncode == 130:
                LOG.info('Import stopped; rerun with %s to resume.', checkpoint)
                return 130
            after = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
            if batch_can_continue(state, after, returncode):
                if returncode:
                    LOG.warning('Batch saved its valid products; %s pages deferred for retry. Continuing with the remaining catalogue.', after['last_batch']['failed_pages'])
            else:
                LOG.error('Batch failed; committed pages retained in %s. Resolve error and rerun.', checkpoint)
                return returncode or 1
        # Give scheduled refreshes a chance to acquire the shared lock.
        time.sleep(5)


if __name__ == '__main__':
    raise SystemExit(main())
