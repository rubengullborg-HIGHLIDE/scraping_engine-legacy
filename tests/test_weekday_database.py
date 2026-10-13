import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scrapers.full_import.weekday import ObservationError, catalog_row, canonical_url
from scripts.weekday_database import database_row, write_catalog, exclusion_reasons
from scripts.weekday_local import read_json


class WeekdayDatabaseTests(unittest.TestCase):
    def setUp(self):
        fixture = read_json(Path(__file__).parent / 'fixtures/weekday_api.json')
        product = fixture['detail']['data']['product']
        product['url'] = '/en-dk/p/men/trousers/chino/' + product['uri']
        self.row, flags = catalog_row(product, fixture['inventory'], canonical_url(product), '2026-10-07T10:00:00Z')
        # The reduced parser fixture omits media; database tests need an eligible row.
        self.row['images'] = ['https://media.weekday.com/test-product.jpg']
        self.row.update(price_observed_at='2026-10-07T10:00:00Z', inventory_observed_at='2026-10-07T10:00:00Z',
                        inventory_complete=not flags, inventory_warnings=flags)
        self.report = {'scan_complete': True, 'limited': False, 'products': [self.row]}

    def test_mapping_excludes_database_identity_and_keeps_source_references_in_raw(self):
        self.row.update(id=123, unexpected='ignored')
        mapped = database_row(self.row)
        self.assertNotIn('id', mapped)
        self.assertNotIn('unexpected', mapped)
        self.assertNotIn('source_metadata', mapped)
        self.assertEqual(mapped['raw']['source_sizes'], self.row['source_sizes'])
        self.assertEqual(mapped['local_inventory'], self.row['local_inventory'])

    def test_nonlocal_rows_rejected(self):
        self.row['aarhus_available'] = None
        with self.assertRaises(ObservationError):
            database_row(self.row)

    def test_partial_images_and_unknown_sizes_are_skipped_before_any_write(self):
        partial = copy.deepcopy(self.row)
        partial.update(source_color_id='1111111001', inventory_complete=False)
        no_images = dict(self.row, source_color_id='1111111002', images=[])
        unknown = copy.deepcopy(self.row)
        unknown['source_color_id'] = '1111111003'
        size = next(iter(unknown['local_inventory']['stores']['weekday-aarhus']['sizes'].values()))
        size.update(stock=None, available=None)
        self.report['products'].extend([partial, no_images, unknown])
        client = Mock()
        with tempfile.TemporaryDirectory() as temp:
            result = write_catalog(self.report, client, Path(temp))
        self.assertEqual(result['requested'], 1)
        self.assertEqual(result['eligibility']['excluded'], 3)
        self.assertEqual(len(client.upsert_products.call_args.args[1]), 1)
        for row in (partial, no_images, unknown):
            with self.assertRaises(ObservationError):
                database_row(row)

    def test_lying_complete_flag_or_inconsistent_totals_are_rejected(self):
        self.row['aarhus_total_stock'] += 1
        self.assertIn('incomplete_or_inconsistent_aarhus_inventory', exclusion_reasons(self.row))

    def test_failed_scan_and_duplicate_identities_never_write(self):
        client = Mock()
        with tempfile.TemporaryDirectory() as temp:
            self.report['scan_complete'] = False
            with self.assertRaises(ObservationError):
                write_catalog(self.report, client, Path(temp))
            self.report.update(scan_complete=True, products=[self.row, self.row])
            with self.assertRaises(ObservationError):
                write_catalog(self.report, client, Path(temp))
        client.upsert_products.assert_not_called()

    def test_interrupted_write_logs_confirmed_batches_and_replay_uses_same_keys(self):
        second = copy.deepcopy(self.row)
        second['source_color_id'] = second['source_parent_id'] + '999'
        self.report['products'].append(second)
        client = Mock()
        client.upsert_products.side_effect = [None, RuntimeError('interrupted')]
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with self.assertRaises(RuntimeError):
                write_catalog(self.report, client, directory, batch_size=1)
            progress = read_json(directory / 'database-write.json')
            self.assertFalse(progress['complete'])
            self.assertEqual(progress['confirmed_written'], 1)
            client.upsert_products.side_effect = None
            result = write_catalog(self.report, client, directory, batch_size=1)
            self.assertTrue(result['complete'])
            self.assertEqual(result['confirmed_written'], 2)
            self.assertEqual(client.upsert_products.call_args_list[0], client.upsert_products.call_args_list[2])

    def test_transport_uses_stable_conflict_key(self):
        from scripts.import_rains_products import SupabaseCatalogClient
        with patch('requests.Session') as session:
            session.return_value.post.return_value.ok = True
            client = SupabaseCatalogClient('https://example.supabase.co', 'sb_secret_fake')
            client.upsert_products('weekday_products', [database_row(self.row)])
            request = session.return_value.post.call_args
            self.assertEqual(request.kwargs['params']['on_conflict'], 'source_parent_id,source_color_id')
            self.assertIn('resolution=merge-duplicates', request.kwargs['headers']['Prefer'])
            self.assertEqual(json.loads(request.kwargs['data'])[0]['source_color_id'], self.row['source_color_id'])

    def test_default_cli_never_connects_to_database(self):
        from scripts.import_weekday_products import main
        with tempfile.TemporaryDirectory() as temp, patch('scripts.weekday_database.connect') as connect:
            with patch('scripts.import_weekday_products.build_catalog', return_value={
                'scan_complete': True, 'database_eligibility': {'eligible': 0, 'excluded': 0},
                'summary': {'partial': 0, 'unknown': 0}}):
                from scripts.weekday_local import atomic_json
                atomic_json(Path(temp) / 'discovery.json', {'mode': 'discovery', 'complete': True, 'products': []})
                self.assertEqual(main(['--work-dir', temp, '--offline']), 0)
            connect.assert_not_called()
