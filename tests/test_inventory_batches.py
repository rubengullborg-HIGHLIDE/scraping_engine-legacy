import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from scripts import run_inventory_batches as runner
from scripts import refresh_store_inventory as refresh


def row(number):
    return {'id': number, 'canonical_url': f'https://example.com/{number}', 'source_parent_id': str(number)}


def stage(database, args):
    for item in database.rows:
        database.patch_row(refresh.STORE_SPECS[database.store], item['id'], {'current_price': 100})
        database.queue_history_observation({'store': database.store, 'product_id': item['id'],
                                           'observed_from': '2026-10-19T10:00:00+00:00'})
    return refresh.RefreshStats(updated=len(database.rows), history_observations=len(database.rows))


class InventoryBatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.json'
        self.database = Mock()
        self.state = {'stores': ['cejf', 'stoy'], 'next_store': 0, 'journal': None,
                      'cycle_id': 'one-cycle', 'started_at': runner.now(), 'batches': [],
                      'totals': dict.fromkeys(runner.COUNTERS, 0), 'active_batch': None,
                      'retry_seconds': 3600, 'jobs': {
                          store: [{'id': store + str(i), 'rows': [row(i)], 'retry_at': 0}
                                  for i in (1, 2)] for store in ('cejf', 'stoy')}}
        self.args = refresh.parse_args([])

    def run_cycle(self, max_batches=0):
        return runner.run_cycle(self.database, self.path, self.state, self.args, max_batches)

    def test_rotates_stores_and_finishes_once(self):
        order = []
        def scrape(database, args):
            order.append(database.store)
            return stage(database, args)
        with patch.dict(refresh.STORE_REFRESHERS, cejf=scrape, stoy=scrape), patch.object(runner, 'put_run'):
            self.assertEqual(self.run_cycle(), 0)
            self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(order, ['cejf', 'stoy', 'cejf', 'stoy'])
        self.assertFalse(any(json.loads(self.path.read_text())['jobs'].values()))

    def test_history_failure_keeps_journal_and_replays_without_scraping(self):
        self.database.flush_history.side_effect = RuntimeError('database unavailable')
        scraper = Mock(side_effect=stage)
        with patch.dict(refresh.STORE_REFRESHERS, cejf=scraper), patch.object(runner, 'put_run'):
            with self.assertRaisesRegex(RuntimeError, 'database unavailable'):
                self.run_cycle(1)
            persisted = json.loads(self.path.read_text())
            self.assertIsNotNone(persisted['journal'])
            self.assertEqual(len(persisted['jobs']['cejf']), 2)
            original = copy.deepcopy(persisted['journal']['history'])
            self.database.flush_history.side_effect = None
            runner.commit(self.database, self.path, persisted)
            self.assertEqual(scraper.call_count, 1)
            self.assertEqual(self.database.queue_history_observation.call_args.args[0], original[0])
            self.assertIsNone(persisted['journal'])
            self.assertEqual(len(persisted['jobs']['cejf']), 1)

    def test_no_product_write_before_journal_is_saved(self):
        def check(*args):
            persisted = json.loads(self.path.read_text())
            self.assertEqual(persisted['journal']['patches'][0]['id'], 1)
            self.assertEqual(persisted['journal']['history'][0]['product_id'], 1)
        self.database.patch_row.side_effect = check
        with patch.dict(refresh.STORE_REFRESHERS, cejf=stage), patch.object(runner, 'put_run'):
            self.assertEqual(self.run_cycle(1), 1)

    def test_failed_rows_are_deferred_while_other_batches_continue(self):
        calls = []
        def scrape(database, args):
            calls.append(database.rows[0]['id'])
            if database.rows[0]['id'] == 1:
                return refresh.RefreshStats(failed=1)
            return stage(database, args)
        with patch.dict(refresh.STORE_REFRESHERS, cejf=scrape, stoy=stage), patch.object(runner, 'put_run'):
            self.assertEqual(self.run_cycle(), 1)
        self.assertEqual(calls, [1, 2])
        self.assertEqual([job['rows'][0]['id'] for job in self.state['jobs']['cejf']], [1])
        self.assertGreater(self.state['jobs']['cejf'][0]['retry_at'], 0)

    def test_rate_limit_defers_entire_store(self):
        error = refresh.StoreRateLimited('HTTP 429')
        error.retry_after_seconds = 172800
        blocked = Mock(side_effect=error)
        with patch.dict(refresh.STORE_REFRESHERS, cejf=blocked, stoy=stage), patch.object(runner, 'put_run'):
            self.assertEqual(self.run_cycle(), 1)
        self.assertEqual(blocked.call_count, 1)
        self.assertFalse(self.state['jobs']['stoy'])
        self.assertTrue(all(job['retry_at'] > runner.time.time() + 172799
                            for job in self.state['jobs']['cejf']))

    def test_failed_adapter_guard_discards_staged_writes(self):
        def fail(database, args):
            stage(database, args)
            raise ValueError('mass disappearance guard')
        with patch.dict(refresh.STORE_REFRESHERS, cejf=fail), patch.object(runner, 'put_run'):
            self.run_cycle(1)
        self.database.patch_row.assert_not_called()
        self.database.queue_history_observation.assert_not_called()

    def test_missing_history_refuses_commit(self):
        def bad(database, args):
            database.patch_row(refresh.STORE_SPECS['cejf'], 1, {})
            return refresh.RefreshStats(updated=1)
        with patch.dict(refresh.STORE_REFRESHERS, cejf=bad), patch.object(runner, 'put_run'):
            with self.assertRaisesRegex(ValueError, 'exactly one history'):
                self.run_cycle(1)
        self.database.patch_row.assert_not_called()

    def test_sibling_rows_are_not_split(self):
        rows = [row(1), row(2), {**row(3), 'canonical_url': row(1)['canonical_url']}]
        self.assertEqual([[r['id'] for r in b] for b in runner.batches('axel', rows, 1)], [[1, 3], [2]])
        rows[2]['source_parent_id'] = '1'
        self.assertEqual([[r['id'] for r in b] for b in runner.batches('magasin', rows, 1)], [[1, 3], [2]])

    def test_interrupted_fetch_keeps_the_same_whole_cycle_record(self):
        self.state['active_batch'] = {'id': 'interrupted'}
        with patch.dict(refresh.STORE_REFRESHERS, cejf=stage), patch.object(runner, 'put_run') as put:
            self.run_cycle(1)
        self.assertTrue(all(call.args[1]['id'] == 'one-cycle' for call in put.call_args_list))
        self.assertTrue(all(call.args[1]['row_limit'] is None for call in put.call_args_list))
        self.assertEqual(put.call_args.args[1]['selected_stores'], ['cejf', 'stoy'])
        self.assertEqual(put.call_args.args[1]['updated_products'], 1)
        self.assertIsNone(put.call_args.args[1]['finished_at'])

    def test_cycle_totals_and_completion_cover_all_batches(self):
        with patch.dict(refresh.STORE_REFRESHERS, cejf=stage, stoy=stage), patch.object(runner, 'put_run') as put:
            self.run_cycle()
        result = put.call_args.args[1]
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['updated_products'], 4)
        self.assertEqual(result['history_observations'], 4)
        self.assertEqual(result['stores_completed'], 2)
        self.assertIsNotNone(result['finished_at'])

    def test_completed_rows_removed_only_after_final_run_write(self):
        with patch.dict(refresh.STORE_REFRESHERS, cejf=stage), \
                patch.object(runner, 'put_run', side_effect=[None, None, RuntimeError('run write failed')]):
            with self.assertRaises(RuntimeError):
                self.run_cycle(1)
        persisted = json.loads(self.path.read_text())
        self.assertEqual(len(persisted['jobs']['cejf']), 2)
        self.assertIsNotNone(persisted['journal'])

    def test_database_failure_flags_whole_cycle_and_keeps_recovery_journal(self):
        self.database.patch_row.side_effect = refresh.SupabasePatchError('database unavailable')
        with patch.dict(refresh.STORE_REFRESHERS, cejf=stage), patch.object(runner, 'put_run') as put:
            with self.assertRaises(refresh.SupabasePatchError):
                self.run_cycle(1)
        persisted = json.loads(self.path.read_text())
        self.assertIsNotNone(persisted['journal'])
        self.assertTrue(persisted['write_failed'])
        self.assertEqual(put.call_args.args[1]['status'], 'failed')
        self.assertEqual(put.call_args.args[1]['id'], 'one-cycle')
        self.assertTrue(put.call_args.args[1]['write_failed'])

    def test_upsert_uses_saved_run_id(self):
        self.database.runs_table = 'store_inventory_refresh_runs'
        payload = {'id': 'saved-id'}
        runner.put_run(self.database, payload)
        kwargs = self.database.session.post.call_args.kwargs
        self.assertEqual(kwargs['json'], payload)
        self.assertIn('resolution=merge-duplicates', kwargs['headers']['Prefer'])

    def test_patch_refuses_newer_or_deleted_product(self):
        database = runner.BatchInventoryClient('https://example.com', 'test',
            history_table='history', runs_table='runs')
        self.addCleanup(database.close)
        response = Mock()
        response.json.return_value = []
        with patch.object(database.session, 'patch', return_value=response) as request:
            with self.assertRaisesRegex(refresh.SupabasePatchError, 'refusing stale replay'):
                database.patch_row(refresh.STORE_SPECS['cejf'], 1, {'updated_at': '2026-10-19T10:00:00Z'})
        self.assertEqual(request.call_args.kwargs['params']['updated_at'], 'lte.2026-10-19T10:00:00Z')

    def test_partial_batch_keeps_only_uncommitted_rows(self):
        self.state['jobs']['cejf'][0]['rows'].append(row(3))
        def partial(database, args):
            database.rows = database.rows[:1]
            stage(database, args)
            return refresh.RefreshStats(updated=1, failed=1, history_observations=1)
        with patch.dict(refresh.STORE_REFRESHERS, cejf=partial), patch.object(runner, 'put_run'):
            self.run_cycle(1)
        retry = self.state['jobs']['cejf'][-1]
        self.assertEqual([r['id'] for r in retry['rows']], [3])
        self.assertNotIn('run', retry)

    def test_cli_rejects_invalid_bounds(self):
        for flag, value in [('--batch-pages', '0'), ('--max-batches', '-1'), ('--retry-seconds', '0')]:
            with self.assertRaises(SystemExit):
                runner.main(['--store', 'cejf', '--checkpoint', str(self.path), flag, value])


if __name__ == '__main__':
    unittest.main()
