import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.probe_weekday_product import (
    AccessBlocked, IncompleteObservation, main, observe_batch, parse_snapshot, read_urls,
)

FIXTURE = Path(__file__).parent / 'fixtures' / 'weekday_probe.json'


class WeekdayProbeTests(unittest.TestCase):
    def setUp(self):
        self.source = json.loads(FIXTURE.read_text())

    def test_live_sample_sizes_and_unknown_counts(self):
        row = parse_snapshot(self.source)
        self.assertEqual((row['source_parent_id'], row['source_color_id']), ('1114252', '1114252035'))
        self.assertEqual(row['current_price'], 449)
        self.assertEqual(list(row['local_inventory']['stores']), ['weekday-aarhus'])
        store = row['local_inventory']['stores']['weekday-aarhus']
        self.assertEqual(len(store['sizes']), 28)
        self.assertEqual(sum(s['available'] for s in store['sizes'].values()), 11)
        self.assertEqual(store['sizes']['25/30'], {'available': False, 'stock': None})
        self.assertTrue(store['sizes']['25/32']['available'])
        self.assertTrue(store['sizes']['27/30']['available'])
        self.assertFalse(store['stock_known'])
        self.assertIsNone(store['total_stock'])
        self.assertIsNone(row['aarhus_total_stock'])
        self.assertIsNone(row['local_total_stock'])
        self.assertEqual(list(row['raw']['local_size_status'].values()).count('Few left'), 9)

    def test_online_availability_does_not_control_local(self):
        for size in self.source['product']['sizeVariants']:
            size['inStock'] = 'no'
        self.assertTrue(parse_snapshot(self.source)['aarhus_available'])

    def test_complete_sold_out_panel_is_valid(self):
        for size in self.source['sizes']:
            size['dot_class'] = 'rounded-full bg-lightGray'
        row = parse_snapshot(self.source)
        self.assertFalse(row['aarhus_available'])
        self.assertIsNone(row['aarhus_total_stock'])

    def test_missing_extra_or_duplicate_local_sizes_fail(self):
        for change in ('missing', 'extra', 'duplicate'):
            with self.subTest(change=change):
                source = copy.deepcopy(self.source)
                if change == 'missing':
                    source['sizes'].pop()
                elif change == 'extra':
                    source['sizes'].append({'label': '99/99', 'dot_class': 'bg-orange'})
                else:
                    source['sizes'].append(source['sizes'][0])
                with self.assertRaises(IncompleteObservation):
                    parse_snapshot(source)

    def test_unknown_stock_marker_and_incomplete_legend_fail(self):
        self.source['sizes'][0]['dot_class'] = 'rounded-full bg-unknown'
        with self.assertRaises(IncompleteObservation):
            parse_snapshot(self.source)
        self.source = json.loads(FIXTURE.read_text())
        self.source['legend'].pop()
        with self.assertRaises(IncompleteObservation):
            parse_snapshot(self.source)

    def test_wrong_colour_or_store_fails(self):
        for key, value in [('inventory_url', '/en-dk/p/men/another-colour/'),
                           ('store_text', 'Weekday Copenhagen'),
                           ('store_address', 'Købmagergade 47')]:
            with self.subTest(key=key):
                source = copy.deepcopy(self.source)
                source[key] = value
                with self.assertRaises(IncompleteObservation):
                    parse_snapshot(source)

    def test_bad_price_currency_or_identity_fails(self):
        for field, value in [('priceAsNumber', float('nan')), ('priceAsNumber', True),
                             ('priceAsNumber', -1), ('price', '449 SEK')]:
            with self.subTest(field=field, value=value):
                source = copy.deepcopy(self.source)
                source['product']['pricing'][field] = value
                with self.assertRaises(IncompleteObservation):
                    parse_snapshot(source)
        self.source['product']['sku'] = '9999999001'
        with self.assertRaises(IncompleteObservation):
            parse_snapshot(self.source)

    def test_replay_is_explicit_and_preserves_observation_time(self):
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('scripts.probe_weekday_product.capture') as capture:
            self.assertEqual(main(['--input', str(FIXTURE)]), 0)
            capture.assert_not_called()
        row = json.loads(stdout.getvalue())
        self.assertEqual(row['observation_mode'], 'replay')
        self.assertEqual(row['observed_at'], '2026-10-07T13:33:05.416000+00:00')

    def test_blocked_capture_returns_failure_and_preserves_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            output.write_text('existing result')
            with patch('scripts.probe_weekday_product.capture', side_effect=IncompleteObservation('HTTP 403')):
                with patch('sys.stderr', io.StringIO()), patch('sys.stdout', io.StringIO()) as stdout:
                    self.assertEqual(main(['--output', str(output)]), 1)
                    self.assertEqual(stdout.getvalue(), '')
            self.assertEqual(output.read_text(), 'existing result')

    def test_batch_keeps_failures_separate_and_continues_to_valid_observation(self):
        with patch('scripts.probe_weekday_product.capture_page', side_effect=[
            AccessBlocked('Product HTTP 403', 403), self.source,
            IncompleteObservation('Partial stock panel'),
        ]), patch('scripts.probe_weekday_product.time.sleep'), patch('sys.stderr', io.StringIO()):
            report = observe_batch(None, ['a', 'b', 'c'], 3)
        self.assertEqual((report['succeeded'], report['blocked'], report['errors']), (1, 1, 1))
        self.assertNotIn('observation', report['results'][0])
        self.assertEqual(report['results'][1]['observation']['source_color_id'], '1114252035')
        self.assertNotIn('observation', report['results'][2])

    def test_rate_limit_stops_remaining_requests(self):
        with patch('scripts.probe_weekday_product.capture_page',
                   side_effect=AccessBlocked('HTTP 429', 429)) as capture:
            with patch('sys.stderr', io.StringIO()):
                report = observe_batch(None, ['a', 'b', 'c'], 3)
        self.assertEqual(capture.call_count, 1)
        self.assertEqual((report['blocked'], report['skipped']), (1, 2))

    def test_batch_input_validation_before_network(self):
        urls = read_urls(FIXTURE.with_name('weekday_urls.txt'))
        self.assertEqual(len(urls), 10)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'urls.txt'
            for content in ['', '\n'.join(urls + [urls[0]]),
                            '\n'.join([urls[0]] * 2), 'https://example.com/product']:
                path.write_text(content)
                with self.assertRaises(IncompleteObservation):
                    read_urls(path)

    def test_failed_batch_saves_diagnostics_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'batch.json'
            report = {'succeeded': 0, 'requested': 10, 'results': []}
            with patch('scripts.probe_weekday_product.capture_batch', return_value=report):
                code = main(['--urls-file', str(FIXTURE.with_name('weekday_urls.txt')),
                             '--output', str(output)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.read_text()), report)


if __name__ == '__main__':
    unittest.main()
