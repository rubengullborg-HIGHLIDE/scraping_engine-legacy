import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scrapers.full_import.weekday import (
    Blocked, ObservationError, canonical_url, catalog_row, discover_catalog, in_scope, local_fields,
)
from scripts.import_weekday_products import build_catalog
from scripts.refresh_weekday_inventory import BatchedPriceClient, apply_observations, observe, refresh
from scripts.weekday_local import atomic_json, cached, read_json


class WeekdayWorkflowTests(unittest.TestCase):
    def setUp(self):
        f = json.loads((Path(__file__).parent / 'fixtures/weekday_api.json').read_text())
        self.product = f['detail']['data']['product']
        self.product.update(categoryUri='men/trousers/chino', url='/en-dk/p/men/trousers/chino/' + self.product['uri'])
        self.inventory = f['inventory']
        self.row, _ = catalog_row(self.product, self.inventory, canonical_url(self.product), '2026-10-07T10:00:00+00:00')
        self.row.update(price_observed_at='old-price', inventory_observed_at='old-stock', inventory_complete=True)
        self.source = {'mode': 'catalog', 'products': [self.row]}
        self.discovery = {'mode': 'discovery', 'complete': True, 'products': [self.product]}
        product, inventory = self.product, self.inventory
        class Client:
            diagnostics = []
            calls = 0
            def fetch_detail(self, url):
                self.calls += 1
                return copy.deepcopy(product)
            def fetch_inventory(self, p):
                self.calls += 1
                return copy.deepcopy(inventory)
        self.client = Client()

    def test_complete_refresh_retains_all_stable_metadata(self):
        self.row['id'] = 'existing-row'
        result = observe(self.row, self.client)
        updated, guards = apply_observations([self.row], [self.row], [result])
        self.assertEqual(guards, [])
        for key in ('id', 'name', 'color', 'images', 'source_metadata', 'source_sizes', 'source_inventory', 'canonical_url'):
            self.assertEqual(updated[0][key], self.row[key])
        self.assertNotEqual(updated[0]['inventory_observed_at'], 'old-stock')
        self.assertEqual(updated[0]['aarhus_total_stock'], 18)

    def test_missing_stock_updates_price_but_preserves_inventory_and_timestamp(self):
        self.client.fetch_inventory = lambda p: []
        result = observe(self.row, self.client)
        updated, guards = apply_observations([self.row], [self.row], [result])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(updated[0]['inventory_observed_at'], 'old-stock')
        self.assertEqual(updated[0]['local_inventory'], self.row['local_inventory'])
        self.assertNotEqual(updated[0]['price_observed_at'], 'old-price')
        self.assertTrue(guards)

    def test_blocked_stock_preserves_valid_price_and_stops_refresh(self):
        def blocked(p):
            raise Blocked('HTTP 403')
        self.client.fetch_inventory = blocked
        with tempfile.TemporaryDirectory() as temp:
            report = refresh(self.source, self.client, Path(temp))
            self.assertEqual(report['summary']['statuses']['blocked'], 1)
            self.assertEqual(report['summary']['price_updates'], 1)
            self.assertEqual(report['summary']['inventory_updates'], 0)
            output = read_json(Path(temp) / 'catalog-refreshed.json')
            self.assertEqual(output['products'][0]['inventory_observed_at'], 'old-stock')

    def test_changed_identity_does_not_patch_price_or_stock(self):
        other = copy.deepcopy(self.product)
        other['sku'] = '9999999999'
        self.client.fetch_detail = lambda url: other
        result = observe(self.row, self.client)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['patch'], {})

    def test_unknown_same_article_size_retains_positive_signal_with_warning(self):
        row = copy.deepcopy(next(r for r in self.inventory if r['store']['storeCode'] == 'DK0651'))
        row.update(variantId=self.product['sku'] + '999', quantity=3)
        fields, flags = local_fields(self.product, self.inventory + [row])
        self.assertIn('unmapped_aarhus_sizes', flags)
        self.assertTrue(fields['aarhus_available'])
        self.assertIsNone(fields['aarhus_total_stock'])
        self.assertEqual(fields['local_total_stock'], 21)
        self.assertNotIn('999', fields['local_inventory']['stores']['weekday-aarhus']['sizes'])

    def test_captured_missing_size_labels_keep_product_available(self):
        fixture = read_json(Path(__file__).parent / 'fixtures/weekday_api_unmapped.json')
        fields, flags = local_fields(fixture['product'], fixture['inventory'])
        self.assertIn('unmapped_aarhus_sizes', flags)
        self.assertTrue(fields['aarhus_available'])
        self.assertIsNone(fields['aarhus_total_stock'])
        source_total = sum(r['quantity'] for r in fixture['inventory'] if r['store']['storeCode'] == 'DK0651')
        self.assertEqual(fields['local_total_stock'], source_total)
        self.assertEqual(len(fields['local_inventory']['stores']['weekday-aarhus']['sizes']), 19)

    def test_capped_store_total_is_unknown_even_if_all_published_sizes_appear(self):
        rows = copy.deepcopy(self.inventory)
        known = {r['variantId'] for r in rows if r['store']['storeCode'] == 'DK0651'}
        template = next(r for r in rows if r['store']['storeCode'] == 'DK0651')
        for size in self.product['sizeVariants']:
            if size['sku'] not in known:
                row = copy.deepcopy(template)
                row.update(variantId=size['sku'], quantity=1)
                rows.append(row)
        foreign = next(r for r in rows if r['store']['storeCode'] != 'DK0651')
        rows.extend(copy.deepcopy(foreign) for _ in range(100-len(rows)))
        fields, flags = local_fields(self.product, rows)
        self.assertIn('possible_inventory_truncation', flags)
        self.assertIsNone(fields['aarhus_total_stock'])
        self.assertFalse(fields['local_inventory']['stores']['weekday-aarhus']['stock_known'])

    def test_mass_unavailability_is_withheld_but_prices_can_update(self):
        rows, results = [], []
        for index in range(10):
            row = copy.deepcopy(self.row)
            row['source_color_id'] = str(index)
            rows.append(row)
            result = observe(self.row, self.client)
            result['source_color_id'] = str(index)
            result['patch'].update(aarhus_available=False, aarhus_total_stock=0)
            results.append(result)
        updated, guards = apply_observations(rows, rows, results)
        self.assertTrue(guards)
        self.assertTrue(all(r['aarhus_available'] for r in updated))
        self.assertTrue(all(r['inventory_observed_at'] == 'old-stock' for r in updated))
        self.assertTrue(all(r['price_observed_at'] != 'old-price' for r in updated))

    def test_unexpected_identity_or_metadata_patch_fails(self):
        result = observe(self.row, self.client)
        result['patch']['description_html'] = 'bad overwrite'
        with self.assertRaises(ObservationError):
            apply_observations([self.row], [self.row], [result])
        result['patch'].pop('description_html')
        result['source_color_id'] = 'new-row'
        with self.assertRaises((ObservationError, KeyError)):
            apply_observations([self.row], [self.row], [result])

    def test_resume_skips_completed_rows_and_rejects_changed_input(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            refresh(self.source, self.client, directory)
            calls = self.client.calls
            refresh(self.source, self.client, directory, resume=True)
            self.assertEqual(self.client.calls, calls)
            changed = copy.deepcopy(self.source)
            changed['products'][0]['current_price'] = 1
            with self.assertRaises(ObservationError):
                refresh(changed, self.client, directory, resume=True)

    def test_discovery_deduplicates_overlap_and_rejects_nonadvancing_pages(self):
        p = self.product
        class Client:
            def get_json(self, url):
                return {'statusCode': 200, 'data': {'pagination': {'totalProducts': 1, 'offset': 0}, 'products': [p]}}
        discovered = discover_catalog(Client())
        self.assertEqual(len(discovered['products']), 1)
        self.assertEqual(len(discovered['pages']), 2)
        class BrokenClient:
            def get_json(self, url):
                return {'statusCode': 200, 'data': {'pagination': {'totalProducts': 2, 'offset': 0}, 'products': [p]}}
        with self.assertRaises(ObservationError):
            discover_catalog(BrokenClient())

    def test_catalog_cache_replay_and_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            first = build_catalog(self.discovery, directory, self.client)
            calls = self.client.calls
            second = build_catalog(self.discovery, directory, None, offline=True)
            self.assertEqual(first['products'], second['products'])
            self.assertEqual(self.client.calls, calls)
            self.assertEqual(second['summary']['success'], 1)
        p = copy.deepcopy(self.product)
        p['categoryUri'] = 'men/accessories/belts'
        self.assertFalse(in_scope(p))
        p['categoryUri'] = 'women/t-shirts-and-tops/oversized'
        p['brandName'] = 'Cheap Monday'
        self.assertTrue(in_scope(p))

    def test_cached_payload_keeps_original_observation_timestamp(self):
        with tempfile.TemporaryDirectory() as temp:
            first = cached(Path(temp), 'inventory', '123', lambda: ['sample'])
            replay = cached(Path(temp), 'inventory', '123', lambda: self.fail('Cache was refetched'), offline=True)
            self.assertEqual(first, replay)

    def test_batched_prices_use_existing_identity_and_preserve_request_time(self):
        discovery = {'products': [self.product], 'product_observed_at': {self.product['sku']: 'batch-time'}}
        client = BatchedPriceClient(self.client, [self.row], discovery)
        result = observe(self.row, client)
        self.assertEqual(self.client.calls, 1)  # inventory only; detail was batched
        self.assertEqual(result['patch']['price_observed_at'], 'batch-time')
        self.assertEqual(result['patch']['current_price'], self.row['current_price'])

    def test_missing_batched_product_falls_back_to_detail_and_does_not_discontinue(self):
        client = BatchedPriceClient(self.client, [self.row], {'products': [], 'product_observed_at': {}})
        result = observe(self.row, client)
        self.assertEqual(self.client.calls, 2)
        self.assertEqual(result['status'], 'success')
        self.assertTrue(result['patch']['aarhus_available'])

    def test_duplicate_input_rows_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ObservationError):
                refresh({'mode': 'catalog', 'products': [self.row, self.row]}, self.client, Path(temp))
            self.assertEqual(self.client.calls, 0)


if __name__ == '__main__':
    unittest.main()
