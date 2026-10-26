"""Sunday HTTP screening must preserve lifecycle without hydrating retired colours."""
import copy
import json
import unittest
from argparse import Namespace
from unittest.mock import Mock, patch

from scrapers.full_import.axel_quint import AxelQuintScraper, IncompleteProduct
from scrapers.stores.kaufmann_variations import KaufmannVariationParseError
from scripts.import_axel_quint_products import import_products, parse_args
from scripts.run_special_catalog_batch import available_catalog_rows
from scripts.sync_store_catalogs import CATALOG_SPECS, importer_command, reconcile_catalog
from tests.test_axel_quint import fixture
from tests.test_kaufmann_variations import variation_payload
from tests.test_store_catalog_sync import FakeLifecycleClient, STARTED_AT, CHECKED_AT


class AvailabilityScreenTests(unittest.TestCase):
    def setUp(self):
        self.scraper = AxelQuintScraper('axel')
        self.addCleanup(self.scraper.close)

    def test_all_unavailable_avoids_browser_and_needs_no_metadata(self):
        snapshot = fixture()
        snapshot['available_only'] = True
        for color in snapshot['colors'].values():
            color['option']['available'] = False
            color.pop('images', None)
            color.pop('description_html', None)
        self.scraper.fetch_availability_snapshot = Mock(return_value=snapshot)
        self.scraper.fetch_snapshot = Mock()
        result = self.scraper.fetch_catalog_snapshot(snapshot['canonical'])
        self.scraper.fetch_snapshot.assert_not_called()
        self.assertIsNone(self.scraper._browser)
        rows = self.scraper.rows_from_snapshot(result)
        self.assertTrue(rows)
        self.assertTrue(all(r['publication_status'] == 'unavailable' and r['aarhus_total_stock'] == 0 for r in rows))
        self.assertTrue(all('name' not in r for r in rows))

    def test_available_colour_hydrates_and_mixed_unavailable_needs_no_images(self):
        snapshot = fixture(index=2)
        snapshot['available_only'] = True
        first = next(iter(snapshot['colors'].values()))
        first['option']['available'] = False
        first.pop('images', None)
        for color in list(snapshot['colors'].values())[1:]:
            color['option']['available'] = True
        self.scraper.fetch_availability_snapshot = Mock(return_value=snapshot)
        self.scraper.fetch_snapshot = Mock(return_value=snapshot)
        rows = self.scraper.rows_from_snapshot(self.scraper.fetch_catalog_snapshot(snapshot['canonical']))
        self.scraper.fetch_snapshot.assert_called_once_with(snapshot['canonical'], available_only=True)
        self.assertNotIn('images', rows[0])
        self.assertTrue(all(r['images'] for r in rows[1:]))

    def test_http_screen_failure_never_falls_back_or_invents_unavailability(self):
        self.scraper.fetch_availability_snapshot = Mock(side_effect=IncompleteProduct('bad response'))
        self.scraper.fetch_snapshot = Mock()
        with self.assertRaises(IncompleteProduct):
            self.scraper.fetch_catalog_snapshot('https://www.axel.dk/produkt/example')
        self.scraper.fetch_snapshot.assert_not_called()

    def test_http_payload_identity_and_boolean_validation(self):
        parent = 'a' * 32
        url = 'https://www.axel.dk/produkt/example'
        tracking = json.dumps({'parentId': parent})
        page = f'''<div x-init="$store.productStore.setup('{parent}')"></div>
        <div data-relewise-tracking-options='{tracking}'></div>'''
        for flag in (False, True, None, 'false', 0):
            response = Mock(url=url, text=page)
            endpoint = Mock(url=f'https://www.axel.dk/widgets/product/variation/{parent}')
            endpoint.json.return_value = {'options': {'color': {'c': {'available': flag}}}}
            self.scraper.session.get = Mock(side_effect=[response, endpoint])
            if isinstance(flag, bool):
                self.assertIs(self.scraper.fetch_availability_snapshot(url)['colors']['c']['option']['available'], flag)
            else:
                with self.assertRaises(IncompleteProduct): self.scraper.fetch_availability_snapshot(url)
        response.text = page.replace(tracking, json.dumps({'parentId': 'b' * 32}))
        self.scraper.session.get = Mock(return_value=response)
        with self.assertRaises(IncompleteProduct): self.scraper.fetch_availability_snapshot(url)


class AvailabilityImportTests(unittest.TestCase):
    def test_known_unavailable_patch_new_unavailable_skip_and_sunday_seen(self):
        snapshot = fixture(index=2)
        snapshot['available_only'] = True
        for color in snapshot['colors'].values(): color['option']['available'] = False
        scraper = AxelQuintScraper('axel')
        self.addCleanup(scraper.close)
        rows = scraper.rows_from_snapshot(snapshot)
        existing = {**rows[0], 'id': 10, 'name': 'Existing name', 'discontinued_at': '2026-01-01T00:00:00Z'}
        lifecycle, client = Mock(), Mock()
        lifecycle.list_rows.return_value = [existing]
        args = parse_args('axel', ['--availability-first', '--url', snapshot['canonical'], '--no-delay'])
        scraper.fetch_catalog_snapshot = Mock(return_value=snapshot)
        with patch('scripts.import_axel_quint_products.AxelQuintScraper', return_value=scraper), \
             patch('scripts.import_axel_quint_products.SupabaseCatalogClient', return_value=client), \
             patch('scripts.sync_store_catalogs.CatalogLifecycleClient', return_value=lifecycle), \
             patch('scripts.import_axel_quint_products.load_dotenv'), \
             patch('scripts.import_axel_quint_products.env', return_value='test'):
            self.assertEqual(import_products('axel', args), 0)
        client.upsert_products.assert_not_called()
        lifecycle.patch_row.assert_called_once()
        table, row_id, payload = lifecycle.patch_row.call_args.args
        self.assertEqual((table, row_id), ('axel_products', 10))
        self.assertEqual(payload['discontinued_at'], existing['discontinued_at'])
        self.assertEqual(payload['publication_status'], 'unavailable')
        self.assertEqual(payload['aarhus_total_stock'], 0)
        self.assertNotIn('raw', payload)
        self.assertNotIn('name', payload)
        before = {**existing, 'updated_at': '2026-08-25T00:00:00Z'}
        after = {**before, **payload, 'updated_at': CHECKED_AT}
        reconciliation = FakeLifecycleClient()
        stats = reconcile_catalog('axel', [before], [after], STARTED_AT, CHECKED_AT,
                                  reconciliation, miss_confirmations=2, min_seen_ratio=.5)
        self.assertEqual(stats.seen, 1)
        self.assertEqual(stats.first_misses, 0)
        self.assertFalse(any(p.get('publication_status') == 'active' for _, _, p in reconciliation.bulk_patches))

    def test_dry_run_does_not_create_database_clients(self):
        snapshot = fixture()
        snapshot['available_only'] = True
        for color in snapshot['colors'].values(): color['option']['available'] = False
        scraper = AxelQuintScraper('axel')
        self.addCleanup(scraper.close)
        scraper.fetch_catalog_snapshot = Mock(return_value=snapshot)
        args = parse_args('axel', ['--availability-first', '--url', snapshot['canonical'], '--dry-run'])
        with patch('scripts.import_axel_quint_products.AxelQuintScraper', return_value=scraper), \
             patch('scripts.import_axel_quint_products.SupabaseCatalogClient') as db, \
             patch('scripts.sync_store_catalogs.CatalogLifecycleClient') as lifecycle, patch('builtins.print'):
            self.assertEqual(import_products('axel', args), 0)
            db.assert_not_called(); lifecycle.assert_not_called()

    def test_sunday_flag_only_for_axel_quint(self):
        args = Namespace(dry_run=False, no_delay=False, limit=None, log_level='INFO')
        for store, spec in CATALOG_SPECS.items():
            self.assertEqual('--availability-first' in importer_command(spec, args), store in {'axel', 'quint'})


class KaufmannScreenTests(unittest.TestCase):
    def test_unavailable_candidate_never_launches_browser(self):
        scraper, client = Mock(), Mock()
        client.resolve_source_parent_id.return_value = 'parent'
        client.fetch_payload.return_value = variation_payload(source_available=False, webshop_stock=5, aarhus_stock=9)
        self.assertEqual(available_catalog_rows(scraper, client, 'url'), [])
        scraper.parse_product_variants_with_js.assert_not_called()

    def test_available_candidate_uses_available_only_browser_mode(self):
        scraper, client = Mock(), Mock()
        client.resolve_source_parent_id.return_value = 'parent'
        client.fetch_payload.return_value = variation_payload(source_available=True, webshop_stock=5, aarhus_stock=9)
        scraper.parse_product_variants_with_js.return_value = [{'source_parent_id': 'parent', 'source_available': True}]
        self.assertEqual(len(available_catalog_rows(scraper, client, 'url')), 1)
        scraper.parse_product_variants_with_js.assert_called_once_with('url', allow_unavailable=True, available_only=True)

    def test_malformed_availability_cannot_be_treated_as_discontinued(self):
        scraper, client = Mock(), Mock()
        client.fetch_payload.return_value = variation_payload(source_available=False, webshop_stock=0, aarhus_stock=0)
        del client.fetch_payload.return_value['options']['color']['color-1']['available']
        with self.assertRaises(KaufmannVariationParseError): available_catalog_rows(scraper, client, 'url')
        scraper.parse_product_variants_with_js.assert_not_called()
