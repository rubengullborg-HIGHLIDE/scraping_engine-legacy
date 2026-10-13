import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.probe_weekday_api import (
    Blocked, ObservationError, STORE_KEY, catalog_row, local_fields,
    price_fields, product_from_response, refresh_patch, run,
)


class WeekdayApiTests(unittest.TestCase):
    def setUp(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/weekday_api.json').read_text())
        self.detail, self.inventory = fixture['detail'], fixture['inventory']
        self.product = self.detail['data']['product']
        self.url = 'https://www.weekday.com/en-dk/p/men/trousers/chino/' + self.product['uri'] + '/'
        self.observed = '2026-10-07T12:00:00+00:00'

    def test_captured_api_price_and_exact_aarhus_stock(self):
        product = product_from_response(self.detail, self.url)
        row, flags = catalog_row(product, self.inventory, self.url, self.observed)
        self.assertEqual(flags, [])
        self.assertEqual(row['current_price'], 449)
        self.assertEqual(row['aarhus_total_stock'], 18)
        self.assertTrue(row['aarhus_available'])
        self.assertTrue(row['local_inventory']['stores'][STORE_KEY]['stock_known'])
        self.assertEqual(len(row['source_sizes']), 7)

    def test_placeholder_rows_are_not_mismatched_sizes(self):
        self.assertTrue(any(r['variantId'] == '' for r in self.inventory))
        fields, flags = local_fields(self.product, self.inventory)
        self.assertEqual(flags, [])
        self.assertEqual(fields['aarhus_total_stock'], 18)

    def test_returned_zero_placeholder_for_aarhus_means_unavailable(self):
        row = copy.deepcopy(next(r for r in self.inventory if r['store']['storeCode'] == 'DK0651'))
        row.update(variantId='', quantity=0)
        fields, flags = local_fields(self.product, [row])
        self.assertEqual(flags, [])
        self.assertEqual(fields['aarhus_total_stock'], 0)
        self.assertIs(fields['aarhus_available'], False)

    def test_missing_store_is_unknown_never_zero(self):
        for rows in ([], [r for r in self.inventory if r['store']['storeCode'] != 'DK0651']):
            fields, flags = local_fields(self.product, rows)
            self.assertIn('aarhus_store_missing', flags)
            self.assertIsNone(fields['aarhus_total_stock'])
            self.assertIsNone(fields['aarhus_available'])
            self.assertTrue(all(v['stock'] is None for v in fields['local_inventory']['stores'][STORE_KEY]['sizes'].values()))

    def test_possible_cap_preserves_unknown_sizes_and_known_positive_stock(self):
        foreign = next(r for r in self.inventory if r['store']['storeCode'] != 'DK0651')
        rows = self.inventory + [copy.deepcopy(foreign) for _ in range(100-len(self.inventory))]
        fields, flags = local_fields(self.product, rows)
        self.assertIn('possible_inventory_truncation', flags)
        self.assertIsNone(fields['aarhus_total_stock'])
        self.assertEqual(fields['local_total_stock'], 18)
        self.assertTrue(fields['aarhus_available'])
        self.assertFalse(fields['local_inventory']['stores'][STORE_KEY]['stock_known'])

    def test_wrong_article_unknown_size_and_invalid_quantity_fail(self):
        for change in ({'articleId': '9999999999'}, {'variantId': '9999999999999'}, {'quantity': -1}, {'quantity': True}):
            rows = copy.deepcopy(self.inventory)
            row = next(r for r in rows if r['store']['storeCode'] == 'DK0651')
            row.update(change)
            with self.subTest(change=change), self.assertRaises(ObservationError):
                local_fields(self.product, rows)

    def test_duplicate_local_variant_fails(self):
        row = next(r for r in self.inventory if r['store']['storeCode'] == 'DK0651')
        with self.assertRaises(ObservationError):
            local_fields(self.product, self.inventory + [row])

    def test_online_stock_never_controls_physical_stock(self):
        self.product['available'] = False
        for size in self.product['sizeVariants']:
            size['inStock'] = 'no'
        self.assertTrue(local_fields(self.product, self.inventory)[0]['aarhus_available'])

    def test_price_mismatch_and_unverified_voucher_fail(self):
        self.product['pricing']['price'] = '1.00 DKK'
        with self.assertRaises(ObservationError):
            price_fields(self.product)
        self.product['pricing']['price'] = '449.00 DKK'
        self.product['voucher']['voucherPercentage'] = 20
        with self.assertRaises(ObservationError):
            price_fields(self.product)

    def test_sale_price_and_previous_price(self):
        self.product['pricing'].update(priceAsNumber=300, price='300.00 DKK')
        fields = price_fields(self.product)
        self.assertEqual((fields['current_price'], fields['list_price']), (300, 449))

    def test_identity_market_and_size_mapping_changes_fail(self):
        for mutate in (
            lambda d: d['data'].update(marketCode='se'),
            lambda d: d['data']['product'].update(sku='9999999999'),
            lambda d: d['data']['product']['sizeVariants'].append(d['data']['product']['sizeVariants'][0]),
        ):
            data = copy.deepcopy(self.detail)
            mutate(data)
            with self.assertRaises(ObservationError):
                product_from_response(data, self.url)

    def test_refresh_only_dynamic_fields_and_existing_identity(self):
        row, _ = catalog_row(self.product, self.inventory, self.url, self.observed)
        row['id'] = 'existing-database-id'
        update, flags = refresh_patch(row, self.product, self.inventory, self.observed)
        self.assertEqual(flags, [])
        self.assertEqual(update['id'], row['id'])
        self.assertNotIn('name', update['patch'])
        self.assertNotIn('source_metadata', update['patch'])
        row['source_color_id'] = '9999999999'
        with self.assertRaises(ObservationError):
            refresh_patch(row, self.product, self.inventory, self.observed)

    def test_partial_stock_refresh_emits_price_only_and_no_inventory_timestamp(self):
        row, _ = catalog_row(self.product, self.inventory, self.url, self.observed)
        update, flags = refresh_patch(row, self.product, [], self.observed)
        self.assertIn('aarhus_store_missing', flags)
        self.assertEqual(set(update['patch']), {'current_price', 'list_price', 'currency'})

    def test_block_stops_batch_without_unavailable_updates(self):
        class Client:
            diagnostics = []
            def fetch(self, url):
                raise Blocked('challenge')
        targets = [{'canonical_url': self.url}] * 2
        with patch('sys.stderr', new_callable=io.StringIO):
            report = run('refresh', targets, Client())
        self.assertEqual([r['status'] for r in report['results']], ['blocked', 'skipped'])
        self.assertEqual(report['updates'], [])


if __name__ == '__main__':
    unittest.main()
