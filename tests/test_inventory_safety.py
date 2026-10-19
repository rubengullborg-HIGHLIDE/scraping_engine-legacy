"""Broken source payloads must not replace the last validated observation."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scrapers.full_import.stoy import StoyScraper
from scrapers.full_import.skagen_clothing import SkagenClothingScraper
from scrapers.source_http import StoreRateLimited
from scripts import refresh_store_inventory as refresh


class InventorySafetyTests(unittest.TestCase):
    def test_missing_inventory_does_not_patch_product_or_history(self):
        product = {'id': 1, 'handle': 'shirt', 'price': 10000,
                   'variants': [{'id': 11, 'title': 'M', 'available': True}]}
        for key, scraper, html in (
            ('stoy', StoyScraper(), '<meta property="og:price:currency" content="DKK">'),
            ('skagen_clothing', SkagenClothingScraper(), '<html></html>'),
        ):
            with self.subTest(store=key):
                database = Mock()
                database.list_rows.return_value = [{'id': 1, 'canonical_url': 'https://example.com/shirt'}]
                args = refresh.parse_args(['--store', key, '--no-delay'])
                with self.assertLogs('refresh_store_inventory', level='ERROR'):
                    stats = refresh.refresh_single_row_store(
                        refresh.STORE_SPECS[key], database, args, scraper,
                        lambda source, row, args: source.product_to_row(product, html))
                self.assertEqual(stats.failed, 1)
                self.assertEqual(stats.updated, 0)
                database.patch_row.assert_not_called()
                database.queue_history_observation.assert_not_called()

    def test_daily_scope_only_selects_active_aarhus_available_rows(self):
        rows = [
            {'id': 1, 'publication_status': 'active', 'aarhus_available': True},
            {'id': 2, 'publication_status': 'active', 'aarhus_available': False},
            {'id': 3, 'publication_status': 'active', 'aarhus_available': None},
            {'id': 4, 'publication_status': 'missing', 'aarhus_available': True},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rows.json'
            path.write_text(json.dumps(rows))
            self.assertEqual(refresh.LocalInventoryClient(path).list_rows(None), rows[:1])
        client = refresh.SupabaseInventoryClient('https://example.com', 'test',
                    history_table='history', runs_table='runs')
        client.close()
        client.session = Mock()
        client.session.get.return_value.json.return_value = []
        client.list_rows(refresh.STORE_SPECS['magasin'])
        params = client.session.get.call_args.kwargs['params']
        self.assertEqual(params['publication_status'], 'eq.active')
        self.assertEqual(params['aarhus_available'], 'eq.true')

    def test_wrapper_arguments_and_shared_rate_limit_type(self):
        args = refresh.parse_args(['--source-transport', 'system-curl', '--all', '--dry-run'])
        self.assertEqual(args.source_transport, 'system-curl')
        self.assertIn('weekday', refresh.STORE_REFRESHERS)
        self.assertIs(refresh.StoreRateLimited, StoreRateLimited)


if __name__ == '__main__':
    unittest.main()
