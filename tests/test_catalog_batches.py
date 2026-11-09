import copy
import fcntl
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import run_catalog_batches as runner
from scripts.catalog_manifest import manifest_items, save_state
from scripts.sync_store_catalogs import reconcile_catalog, parse_timestamp
from tests.test_store_catalog_sync import rains_row, FakeLifecycleClient, STARTED_AT, CHECKED_AT


class ManifestTests(unittest.TestCase):
    def test_frozen_order_and_metadata_survive_discovery_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(catalog_manifest=str(Path(directory)/'items.json'), url=[])
            discover = Mock(return_value=[{'handle':'a', '_discovery_collections':['men']}, {'handle':'b'}])
            original = manifest_items(args, 'rains', discover)
            discover.return_value = [{'handle':'new'}]
            self.assertEqual(original, manifest_items(args, 'rains', discover))
            self.assertEqual(1, discover.call_count)
            with self.assertRaisesRegex(ValueError, 'mismatch'):
                manifest_items(args, 'stoy', discover)

    def test_empty_discovery_never_creates_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'items.json'
            args = SimpleNamespace(catalog_manifest=str(target), url=[])
            with self.assertRaisesRegex(ValueError, 'Empty'):
                manifest_items(args, 'rains', lambda: [])
            self.assertFalse(target.exists())

    def test_feed_based_importers_fetch_live_values_from_saved_manifest(self):
        for store, class_name in [('cejf', 'CejfScraper'), ('suitclub', 'SuitClubScraper')]:
            with self.subTest(store=store), tempfile.TemporaryDirectory() as directory:
                module = importlib.import_module(f'scripts.import_{store}_products')
                manifest = Path(directory)/'items.json'
                save_state(manifest, dict(version=1, store=store,
                    items=[{'handle':'item', 'id':1, 'price':1}]))
                with patch('sys.argv', ['import', '--catalog-manifest', str(manifest), '--no-delay']):
                    args = module.parse_args()
                scraper = Mock()
                scraper.fetch_product.return_value = {'handle':'item', 'id':1, 'price':2}
                scraper.fetch_inventory_snapshot.return_value = {'products':{}, 'locations':{}}
                scraper._handle_from_url.side_effect = lambda value: value
                scraper.product_to_row.return_value = {'current_price':2}
                with patch(f'scrapers.full_import.{store}.{class_name}', return_value=scraper), \
                     patch.object(module, 'env', return_value='test'), \
                     patch.object(module, 'SupabaseCatalogClient'):
                    self.assertEqual(0, getattr(module, f'import_{store}_products')(args))
                scraper.discover_products.assert_not_called()
                self.assertEqual(2, scraper.product_to_row.call_args.args[0]['price'])


class CatalogBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'cycle.json'
        self.state = dict(cycle_id='cycle', batch_pages=1, jobs={'rains':{}}, stores=['rains'])
        self.client = Mock()
        self.recorder = Mock()
        self.recorder.session.get.return_value.json.return_value = []
        self.recorder.start.return_value = 42
        self.args = SimpleNamespace(batch_timeout=60, miss_confirmations=2, min_seen_ratio=.5)
        self.old = rains_row(1, updated_at='2020-01-01T00:00:00+00:00')
        self.fresh = rains_row(1, updated_at='2099-01-01T00:00:00+00:00')
        self.manifest = self.path.parent/(self.path.name+'.manifests')/'cycle'/'rains.json'

    def child(self, command, timeout):
        self.assertIn('--catalog-manifest', command)
        save_state(self.manifest, {'version':1, 'store':'rains', 'items':[{'handle':'a'}, {'handle':'b'}]})
        return 0

    def test_resume_stable_offset_and_single_run_reconcile_only_after_last_batch(self):
        self.client.list_rows.side_effect = [[self.old], [self.fresh], [self.fresh], [self.fresh]]
        with patch.object(runner, 'run_child', side_effect=self.child) as child, patch.object(runner, 'apply_plan') as apply:
            runner.process_store(self.state, 'rains', self.path, self.client, self.recorder, self.args)
            apply.assert_not_called()
            state = json.loads(self.path.read_text())
            self.assertEqual(1, state['jobs']['rains']['offset'])
            runner.process_store(state, 'rains', self.path, self.client, self.recorder, self.args)
            self.assertEqual('1', child.call_args.args[0][-1])
            self.assertTrue(state['jobs']['rains']['done'])
            self.assertEqual(1, self.recorder.start.call_count)
            self.assertEqual(1, apply.call_count)

    def test_failed_import_never_advances_or_reconciles(self):
        self.client.list_rows.return_value = [self.old]
        with patch.object(runner, 'run_child', return_value=1), patch.object(runner, 'apply_plan') as apply:
            with self.assertRaisesRegex(RuntimeError, 'retained'):
                runner.process_store(self.state, 'rains', self.path, self.client, self.recorder, self.args)
        self.assertEqual(0, json.loads(self.path.read_text())['jobs']['rains']['offset'])
        apply.assert_not_called()

    def test_reconciliation_failure_replays_saved_plan_without_importing_again(self):
        save_state(self.manifest, {'version':1, 'store':'rains', 'items':[{'handle':'a'}]})
        job = dict(before=[self.old], started_at='2020-02-01T00:00:00+00:00',
                   offset=1, total=1, observed={'1':self.fresh['updated_at']}, run_id=42)
        self.state['jobs']['rains'] = job
        self.client.list_rows.return_value = [self.fresh]
        with patch.object(runner, 'apply_plan', side_effect=RuntimeError('connection lost')):
            with self.assertRaises(RuntimeError):
                runner.process_store(self.state, 'rains', self.path, self.client, self.recorder, self.args)
        saved = json.loads(self.path.read_text())
        self.assertIn('plan', saved['jobs']['rains'])
        with patch.object(runner, 'run_child') as child, patch.object(runner, 'apply_plan') as apply:
            runner.process_store(saved, 'rains', self.path, self.client, self.recorder, self.args)
            child.assert_not_called()
            apply.assert_called_once()
        self.assertTrue(saved['jobs']['rains']['done'])

    def test_recovery_refuses_newer_product_before_history_write(self):
        self.client.session.patch.return_value.json.return_value = []
        plan = dict(checked_at=CHECKED_AT, patches=[dict(table='rains_products', id=1, payload={})], history=[{}])
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            runner.apply_plan(self.client, plan)
        self.client.insert_history.assert_not_called()

    def test_daily_updates_neither_count_as_seen_nor_shield_from_misses(self):
        before = [rains_row(i, updated_at='2020-01-01T00:00:00+00:00') for i in (1,2,3)]
        after = copy.deepcopy(before)
        after[0]['updated_at'] = '2026-08-26T10:30:00+00:00'
        after[1]['updated_at'] = '2026-08-26T10:40:00+00:00'  # daily refresh only
        client = FakeLifecycleClient()
        stats = reconcile_catalog('rains', before, after, STARTED_AT, CHECKED_AT, client,
            miss_confirmations=2, min_seen_ratio=.5, seen_product_ids={1},
            observed_at={1:after[0]['updated_at']})
        self.assertEqual(1, stats.seen)
        # Row 2 was only touched by the daily inventory refresh. That is not a
        # catalogue sighting, so it records a miss just like row 3.
        self.assertEqual([2, 3], [row_id for _, row_id, _ in client.row_patches])

    def test_newer_daily_unavailability_is_not_reactivated(self):
        row = rains_row(1, updated_at='2026-08-26T10:40:00+00:00', publication_status='unavailable')
        client = FakeLifecycleClient()
        reconcile_catalog('rains', [row], [row], STARTED_AT, CHECKED_AT, client,
            miss_confirmations=2, min_seen_ratio=.5, seen_product_ids={1},
            observed_at={1:'2026-08-26T10:30:00+00:00'})
        # Seen, so its miss counter resets, but it is not reactivated over
        # the newer daily observation.
        self.assertEqual([('rains_products', [1], {'last_seen_in_catalog_at': CHECKED_AT,
                                                    'consecutive_catalog_misses': 0})], client.bulk_patches)

    def test_tick_waits_for_inventory_lock_then_runs_catalog(self):
        args = SimpleNamespace(checkpoint=str(self.path), store=['rains'], batch_pages=1,
                               miss_confirmations=2, min_seen_ratio=.5)
        with patch.object(runner, 'LOCK', str(self.path.parent/'inventory.lock')), \
             patch.object(runner.sync, 'env', return_value='test'), \
             patch.object(runner.sync, 'CatalogLifecycleClient', return_value=self.client), \
             patch.object(runner.sync, 'CatalogSyncRunRecorder', return_value=self.recorder), \
             patch.object(runner, 'process_store') as process, \
             patch.object(runner.time, 'sleep'), \
             patch.object(runner.fcntl, 'flock', side_effect=[None, BlockingIOError, None]) as lock:
            self.assertEqual(0, runner.tick(args))
            process.assert_called_once()
            self.assertEqual(fcntl.LOCK_EX, lock.call_args_list[-1].args[1])

    def test_scheduled_new_cycles_only_start_on_sunday_after_1215_utc(self):
        from datetime import datetime
        def timestamp(value):
            return datetime.fromisoformat(value).timestamp()
        self.assertFalse(runner.catalog_cycle_due(timestamp('2026-10-20T12:15:00+00:00'), None))
        self.assertFalse(runner.catalog_cycle_due(timestamp('2026-10-25T12:14:59+00:00'), None))
        self.assertTrue(runner.catalog_cycle_due(timestamp('2026-10-25T12:15:00+00:00'), None))
        old = {'next_cycle_at':timestamp('2026-10-26T14:15:00+00:00')}
        self.assertTrue(runner.catalog_cycle_due(timestamp('2026-10-25T12:15:00+00:00'), old))
        future = {'next_cycle_at':timestamp('2026-11-01T12:15:00+00:00')}
        self.assertFalse(runner.catalog_cycle_due(timestamp('2026-10-25T13:00:00+00:00'), future))

    def test_scheduled_empty_checkpoint_waits_on_weekday_without_connecting(self):
        from datetime import datetime
        args = SimpleNamespace(checkpoint=str(self.path), store=['rains'], batch_pages=1,
                               miss_confirmations=2, min_seen_ratio=.5, scheduled=True)
        with patch.object(runner.time, 'time', return_value=datetime(2026,10,20,tzinfo=runner.timezone.utc).timestamp()), \
             patch.object(runner.sync, 'CatalogLifecycleClient') as client:
            self.assertEqual(0,runner.tick(args))
        client.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_stop_terminates_child_group_before_propagating(self):
        child = Mock(pid=12345)
        child.wait.side_effect = [KeyboardInterrupt, 0, 0]
        with patch.object(runner.subprocess, 'Popen', return_value=child), patch('os.killpg') as kill:
            with self.assertRaises(KeyboardInterrupt):
                runner.run_child(['import'], 60)
            self.assertEqual([(12345, runner.signal.SIGTERM), (12345, runner.signal.SIGKILL)],
                             [call.args for call in kill.call_args_list])
        self.assertEqual(3, child.wait.call_count)

    def test_tick_resumes_rotation_and_releases_lock_between_stores(self):
        args = SimpleNamespace(checkpoint=str(self.path), store=['rains', 'stoy'], batch_pages=1,
                               miss_confirmations=2, min_seen_ratio=.5)
        state = dict(version=2, cycle_id='retained', stores=args.store, batch_pages=1,
            settings=dict(batch_pages=1, miss_confirmations=2, min_seen_ratio=.5),
            next_store=1, next_cycle_at=runner.time.time() + 3600, jobs={'rains':{}, 'stoy':{}})
        args.scheduled = True
        save_state(self.path, state)
        lock_path = str(self.path.parent/'inventory.lock')
        order = []
        def process(state, store, *unused):
            self.assertEqual('retained', state['cycle_id'])
            order.append(store)
        def handoff(seconds):
            with open(lock_path, 'a') as other:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with patch.object(runner, 'LOCK', lock_path), \
             patch.object(runner.sync, 'env', return_value='test'), \
             patch.object(runner.sync, 'CatalogLifecycleClient', return_value=self.client), \
             patch.object(runner.sync, 'CatalogSyncRunRecorder', return_value=self.recorder), \
             patch.object(runner, 'process_store', side_effect=process), \
             patch.object(runner.time, 'sleep', side_effect=handoff):
            self.assertEqual(0, runner.tick(args))
        self.assertEqual(['stoy', 'rains'], order)

    def test_finished_cycle_waits_until_weekly_due_time(self):
        args = SimpleNamespace(checkpoint=str(self.path), store=['rains'], batch_pages=1,
                               miss_confirmations=2, min_seen_ratio=.5)
        save_state(self.path, dict(version=2, stores=args.store,
            settings=dict(batch_pages=1, miss_confirmations=2, min_seen_ratio=.5),
            next_cycle_at=runner.time.time() + 3600, jobs={'rains':{'done':True}}))
        with patch.object(runner.sync, 'CatalogLifecycleClient') as client:
            self.assertEqual(0, runner.tick(args))
            client.assert_not_called()

    def test_partial_sweep_updates_seen_rows_but_does_not_advance_misses(self):
        before = [rains_row(i, updated_at='2020-01-01T00:00:00+00:00') for i in (1, 2)]
        after = copy.deepcopy(before)
        after[0]['updated_at'] = '2026-08-26T10:30:00+00:00'
        client = FakeLifecycleClient()
        stats = reconcile_catalog('rains', before, after, STARTED_AT, CHECKED_AT, client,
            miss_confirmations=2, min_seen_ratio=.5, seen_product_ids={1},
            observed_at={1: after[0]['updated_at']}, advance_misses=False)
        self.assertTrue(stats.missing_check_skipped)
        self.assertEqual([], client.row_patches)
        self.assertEqual([1], client.bulk_patches[0][1])

    def test_partial_importer_exit_advances_batch_and_marks_job_incomplete(self):
        save_state(self.manifest, {'version':1, 'store':'rains', 'items':[{'handle':'a'}, {'handle':'b'}]})
        self.state['jobs']['rains'] = dict(before=[self.old], started_at='2020-02-01T00:00:00+00:00',
                                           offset=0, observed={}, run_id=42)
        self.client.list_rows.return_value = [self.old]
        with patch.object(runner, 'run_child', return_value=runner.EXIT_PARTIAL):
            runner.process_store(self.state, 'rains', self.path, self.client, self.recorder, self.args)
        job = self.state['jobs']['rains']
        self.assertEqual(1, job['offset'])
        self.assertEqual(1, job['incomplete'])
        self.assertNotIn('retry_at', job)

    def test_overdue_unfinished_cycle_is_superseded_by_a_new_one(self):
        args = SimpleNamespace(checkpoint=str(self.path), store=['rains', 'stoy'], batch_pages=1,
                               miss_confirmations=2, min_seen_ratio=.5, scheduled=True)
        old = dict(version=2, cycle_id='stuck', stores=args.store, batch_pages=1,
            settings=dict(batch_pages=1, miss_confirmations=2, min_seen_ratio=.5), next_store=0,
            next_cycle_at=runner.time.time() - 60,
            jobs={'rains': {'done': True, 'run_id': 1}, 'stoy': {'run_id': 2, 'offset': 3, 'error': 'exited 1'}})
        save_state(self.path, old)
        stale_manifests = self.path.parent / (self.path.name + '.manifests') / 'stuck'
        stale_manifests.mkdir(parents=True)
        seen = []
        with patch.object(runner, 'LOCK', str(self.path.parent/'inventory.lock')), \
             patch.object(runner.sync, 'env', return_value='test'), \
             patch.object(runner.sync, 'CatalogLifecycleClient', return_value=self.client), \
             patch.object(runner.sync, 'CatalogSyncRunRecorder', return_value=self.recorder), \
             patch.object(runner, 'process_store', side_effect=lambda state, *a: seen.append(state['cycle_id'])), \
             patch.object(runner.time, 'sleep'):
            self.assertEqual(0, runner.tick(args))
        # Only the unfinished store's run is closed, as partial.
        self.recorder.finish.assert_called_once()
        self.assertEqual(2, self.recorder.finish.call_args.args[0])
        self.assertEqual('partial', self.recorder.finish.call_args.kwargs['status'])
        self.assertFalse(stale_manifests.exists())
        new_state = json.loads(self.path.read_text())
        self.assertNotEqual('stuck', new_state['cycle_id'])
        self.assertGreater(new_state['next_cycle_at'], runner.time.time())
        self.assertTrue(seen and all(cycle == new_state['cycle_id'] for cycle in seen))


if __name__ == '__main__':
    unittest.main()
