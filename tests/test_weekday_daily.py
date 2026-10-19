import json
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

from scrapers.full_import.weekday import Blocked, catalog_row, canonical_url
from scripts import refresh_weekday_database as daily
from scripts import refresh_store_inventory as runner


class WeekdayDailyTests(unittest.TestCase):
    def setUp(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/weekday_api.json').read_text())
        self.product = fixture['detail']['data']['product']
        self.product.update(categoryUri='men/trousers/chino',
                            url='/en-dk/p/men/trousers/chino/' + self.product['uri'])
        self.inventory = fixture['inventory']
        self.row, _ = catalog_row(self.product, self.inventory, canonical_url(self.product),
                                  '2026-10-07T10:00:00+00:00')
        self.row.update(id=42, inventory_complete=True)
        self.args = Namespace(offset=0, limit=None, dry_run=False, no_history=False)
        self.db = Mock()
        self.client = Mock(diagnostics=[])
        self.client.detail_observed_at = None
        self.client.fetch_detail.return_value = self.product
        self.client.fetch_inventory.return_value = self.inventory

    def run_refresh(self):
        with patch.object(daily, 'list_rows', return_value=[self.row]), \
             patch.object(daily, 'Client', return_value=self.client):
            return daily.refresh_store(self.db, self.args)

    def test_registered_for_daily_but_not_generic_catalogue(self):
        self.assertIn('weekday', runner.STORE_REFRESHERS)
        self.assertEqual(runner.parse_args(['--store', 'weekday', '--dry-run']).store, ['weekday'])
        from scripts.sync_store_catalogs import CATALOG_SPECS
        self.assertNotIn('weekday', CATALOG_SPECS)

    def test_query_uses_weekday_schema_and_only_aarhus_stock(self):
        self.db.session.get.return_value.json.return_value = []
        self.assertEqual(daily.list_rows(self.db), [])
        params = self.db.session.get.call_args.kwargs['params']
        self.assertEqual(params['aarhus_available'], 'eq.true')
        self.assertNotIn('publication_status', params)
        self.assertNotIn('publication_status', params['select'])

    def test_updates_existing_id_dynamic_fields_and_records_history(self):
        stats = self.run_refresh()
        spec, row_id, payload = self.db.patch_row.call_args.args
        self.assertEqual((spec.table, row_id), ('weekday_products', 42))
        self.assertNotIn('name', payload)
        self.assertNotIn('raw', payload)
        self.assertNotIn('refresh_status', payload)
        self.assertIn('inventory_checked_at', payload)
        self.assertEqual(payload['inventory_checked_at'], payload['updated_at'])
        self.assertNotIn('price_observed_at', payload)
        self.assertNotIn('inventory_observed_at', payload)
        self.assertEqual(stats.history_observations, 1)
        self.assertEqual(self.db.queue_history_observation.call_args.args[0]['store'], 'weekday')
        self.client.close.assert_called_once()

    def test_blocked_inventory_retains_entire_previous_observation(self):
        self.client.fetch_inventory.side_effect = Blocked('HTTP 403')
        stats = self.run_refresh()
        self.db.patch_row.assert_not_called()
        self.assertEqual(stats.failed, 1)
        self.db.queue_history_observation.assert_not_called()

    def test_capped_inventory_does_not_overwrite_known_stock(self):
        self.client.fetch_inventory.return_value = self.inventory + [
            {'articleId': self.product['sku'], 'store': {'storeCode': 'OTHER'}}
        ] * 100
        stats = self.run_refresh()
        self.assertEqual(stats.failed, 1)
        self.db.patch_row.assert_not_called()

    def test_dry_run_never_writes(self):
        self.args.dry_run = True
        self.run_refresh()
        self.db.patch_row.assert_not_called()
        self.db.queue_history_observation.assert_not_called()

    def test_db_failure_propagates(self):
        self.db.patch_row.side_effect = runner.SupabasePatchError('failed')
        with self.assertRaises(runner.SupabasePatchError):
            self.run_refresh()

    def test_mass_sellout_guard_preserves_stock(self):
        rows = [dict(self.row, id=i, source_color_id=f'1325081{i:03d}') for i in range(10)]
        def empty_stock(row, client):
            return {'source_parent_id': row['source_parent_id'], 'source_color_id': row['source_color_id'],
                    'attempted_at': '2026-10-13T12:00:00+00:00', 'warnings': [], 'errors': [],
                    'status': 'success', 'patch': {'local_inventory': {'stores': {}},
                    'aarhus_available': False, 'inventory_observed_at': '2026-10-13T12:00:00+00:00'}}
        with patch.object(daily, 'list_rows', return_value=rows), \
             patch.object(daily, 'Client', return_value=self.client), \
             patch.object(daily, 'observe', side_effect=empty_stock):
            stats = daily.refresh_store(self.db, self.args)
        self.assertEqual(stats.failed, 10)
        self.db.patch_row.assert_not_called()


if __name__ == '__main__':
    unittest.main()
