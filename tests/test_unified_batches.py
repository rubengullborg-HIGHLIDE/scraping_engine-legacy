import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import run_inventory_batches as inventory
from scripts import run_catalog_batches as catalog
from scripts import run_special_catalog_batch as special_catalog
from scripts import special_store_batches as special
from scripts import refresh_kaufmann_inventory as kaufmann
from scripts import refresh_weekday_database as weekday
from scrapers.source_http import StoreRateLimited
from tests.test_kaufmann_variations import product_row, variation_payload
from tests import test_weekday_daily as weekday_fixtures


class UnifiedBatchTests(unittest.TestCase):
    def test_kaufmann_commit_routes_history_to_shared_table(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Mock()
            database._table_url.side_effect = lambda table: table
            state = dict(stores=['kaufmann'], next_store=0, cycle_id='cycle', started_at=inventory.now(),
                batches=[], totals=dict.fromkeys(inventory.COUNTERS, 0), retry_seconds=3600,
                active_batch=None, jobs={'kaufmann':[dict(id='job',rows=[{'id':1}],retry_at=0)]},
                journal=dict(store='kaufmann', started_at=inventory.now(), error=None,
                    patches=[dict(id=1,payload={})], history=[{'store':'kaufmann','product_id':1}],
                    result=dict.fromkeys(inventory.COUNTERS,0)))
            with patch.object(inventory,'put_run'):
                inventory.commit(database, Path(directory)/'state.json', state)
            database.queue_history_observation.assert_called_once_with({'store':'kaufmann','product_id':1})
            database.session.post.assert_not_called()
            self.assertEqual([],state['jobs']['kaufmann'])

    def test_price_only_weekday_commit_retains_job_for_inventory_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            state = dict(stores=['weekday'], next_store=0, cycle_id='cycle', started_at=inventory.now(),
                batches=[], totals=dict.fromkeys(inventory.COUNTERS, 0), retry_seconds=3600,
                active_batch=None, jobs={'weekday':[dict(id='job',rows=[{'id':1}],retry_at=0)]},
                journal=dict(store='weekday', started_at=inventory.now(), error='partial',
                    patches=[dict(id=1,payload={'price_observed_at':inventory.now()})], history=[],
                    result=dict.fromkeys(inventory.COUNTERS,0)))
            with patch.object(inventory,'put_run'):
                inventory.commit(Mock(), Path(directory)/'state.json', state)
            self.assertEqual(1,len(state['jobs']['weekday']))
            self.assertGreater(state['jobs']['weekday'][0]['retry_at'],0)

    def test_both_registries_have_fourteen_stores(self):
        self.assertEqual(14, len(inventory.SPECS))
        self.assertEqual(set(inventory.SPECS), set(catalog.STORES))

    def test_kaufmann_adapter_reuses_parser_and_shared_history(self):
        staged = inventory.StagedClient('kaufmann', [product_row()])
        client = Mock()
        client.fetch_payload.return_value = variation_payload(source_available=True, webshop_stock=4, aarhus_stock=3)
        with patch.object(kaufmann, 'KaufmannVariationClient', return_value=client):
            stats, candidates = special.run_kaufmann(staged, SimpleNamespace(no_delay=True))
        self.assertEqual(1, stats.pages)
        self.assertEqual([], candidates)
        self.assertEqual(3, staged.patches[0]['payload']['aarhus_total_stock'])
        self.assertNotIn('source_url', staged.patches[0]['payload'])
        self.assertEqual(785, staged.history[0]['product_id'])
        self.assertEqual('kaufmann', staged.history[0]['store'])
        self.assertEqual(staged.patches[0]['payload']['aarhus_inventory'], staged.history[0]['local_inventory'])

    def test_first_missing_kaufmann_colour_has_no_false_history_observation(self):
        row = product_row(source_color_id='gone')
        staged = inventory.StagedClient('kaufmann', [row])
        client = Mock()
        client.fetch_payload.return_value = variation_payload(source_available=True, webshop_stock=1, aarhus_stock=1)
        with patch.object(kaufmann, 'KaufmannVariationClient', return_value=client):
            special.run_kaufmann(staged, SimpleNamespace(no_delay=True))
        self.assertEqual(1, staged.patches[0]['payload']['consecutive_source_misses'])
        self.assertNotIn('aarhus_inventory', staged.patches[0]['payload'])
        self.assertEqual([], staged.history)

    def test_kaufmann_rate_limit_escapes_adapter_and_stops_source(self):
        staged = inventory.StagedClient('kaufmann', [product_row()])
        client = Mock()
        client.fetch_payload.side_effect = StoreRateLimited('HTTP 429')
        with patch.object(kaufmann, 'KaufmannVariationClient', return_value=client):
            with self.assertRaises(StoreRateLimited):
                special.run_kaufmann(staged, SimpleNamespace(no_delay=True))
        self.assertEqual([], staged.patches)
        client.close.assert_called_once()

    def test_weekday_partial_price_does_not_complete_inventory_job(self):
        patches = [{'id':1, 'payload':{'price_observed_at':'2026-10-19T12:00:00Z'}},
                   {'id':2, 'payload':{'inventory_checked_at':'2026-10-19T12:00:00Z'}}]
        self.assertEqual({2}, special.completed_ids('weekday', patches))

    def test_weekday_replay_guards_standard_updated_at(self):
        database = inventory.BatchInventoryClient('https://example.test', 'test', history_table='history', runs_table='runs')
        self.addCleanup(database.close)
        response = Mock()
        response.json.return_value = [{'id':1}]
        values = dict(updated_at='2026-10-19T12:00:00Z', inventory_checked_at='2026-10-19T12:00:00Z')
        with patch.object(database.session, 'patch', return_value=response) as send:
            database.patch_row(special.SPECS['weekday'], 1, values)
        params = send.call_args.kwargs['params']
        self.assertEqual('lte.' + values['updated_at'], params['updated_at'])

    def test_weekday_adapter_uses_frozen_rows_and_defers_on_block(self):
        fixture = weekday_fixtures.WeekdayDailyTests(); fixture.setUp()
        staged = inventory.StagedClient('weekday', [fixture.row])
        with patch.object(weekday, 'Client', return_value=fixture.client), patch.object(weekday, 'list_rows') as read:
            stats = special.run(staged, fixture.args)
        read.assert_not_called()
        self.assertEqual(1, stats.history_observations)
        fixture.client.fetch_detail.side_effect = __import__('scrapers.full_import.weekday', fromlist=['Blocked']).Blocked('blocked')
        with patch.object(weekday, 'Client', return_value=fixture.client):
            with self.assertRaises(StoreRateLimited):
                special.run(inventory.StagedClient('weekday', [fixture.row]), fixture.args)

    def test_catalog_journal_replay_does_not_repeat_source_or_missing_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory)/'kaufmann.json'
            path = special_catalog.report_path(manifest, 0)
            plan = dict(store='kaufmann', limit=10, done=False, patches=[], history=[], new_rows=[])
            path.write_text(json.dumps(plan))
            database = Mock()
            with patch.object(special_catalog.sync, 'env', return_value='test'), \
                 patch.object(special_catalog, 'BatchInventoryClient', return_value=database), \
                 patch.object(special_catalog, 'prepare') as prepare, \
                 patch.object(special_catalog, 'apply', return_value={'1':'2026-10-19T12:00:00Z'}) as apply:
                args = ['--store','kaufmann','--catalog-manifest',str(manifest),'--offset','0','--limit','10']
                self.assertEqual(0, special_catalog.main(args))
                self.assertEqual(0, special_catalog.main(args))
                prepare.assert_not_called()
                apply.assert_called_once()
            self.assertTrue(json.loads(path.read_text())['done'])

    def test_completed_special_catalog_never_uses_generic_absence_reconciliation(self):
        for store in special.SPECS:
            with self.subTest(store=store), tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'state.json'
                job=dict(before=[{'id':1}], run_id=1, offset=1, total=1, observed={'1':'2026-10-19T12:00:00Z'})
                state=dict(cycle_id='cycle', jobs={store:job})
                manifest=path.parent/(path.name+'.manifests')/'cycle'/f'{store}.json'
                manifest.parent.mkdir(parents=True)
                manifest.write_text(json.dumps({'items':['url']}))
                recorder=Mock()
                with patch.object(catalog,'catalog_rows',return_value=[{'id':1}]), \
                     patch.object(catalog.sync,'reconcile_catalog') as reconcile:
                    catalog.process_store(state,store,path,Mock(),recorder,SimpleNamespace())
                    reconcile.assert_not_called()
                self.assertTrue(job['done'])
                self.assertEqual('success',recorder.finish.call_args.kwargs['status'])


if __name__ == '__main__':
    unittest.main()
