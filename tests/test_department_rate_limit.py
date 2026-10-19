import unittest
from unittest.mock import Mock, patch

from scrapers.full_import.department_stores import PublicScraper
from scrapers.source_http import StoreRateLimited, stop_on_source_rate_limit
from scripts import import_axel_quint_products as imports
from scripts import refresh_department_inventory as daily
from scripts import refresh_store_inventory as refresh
from scripts import sync_store_catalogs as sunday


class DepartmentRateLimitTests(unittest.TestCase):
    def test_access_denied_stops_store_without_retrying_pages(self):
        scraper = PublicScraper(request_delay=0)
        self.addCleanup(scraper.close)
        scraper.BASE_URL = 'https://example.com'
        response = Mock(status_code=403, url='https://example.com/product')
        with patch.object(scraper.session, 'get', return_value=response):
            with self.assertRaisesRegex(StoreRateLimited, 'HTTP 403 access denied'):
                scraper.get('/product')
        response.close.assert_called_once()

    def test_transport_stops_429_even_with_retry_after_and_spaces_actual_requests(self):
        scraper = PublicScraper()
        self.addCleanup(scraper.close)
        scraper.BASE_URL = 'https://example.com'
        self.assertEqual(scraper.request_delay, 3)
        retry = scraper.session.get_adapter(scraper.BASE_URL).max_retries
        self.assertFalse(retry.is_retry('GET', 429, has_retry_after=True))
        self.assertIn(stop_on_source_rate_limit, scraper.session.hooks['response'])
        response = Mock(status_code=200, url='https://example.com/product')
        with patch.object(scraper.session, 'get', return_value=response), \
                patch('scrapers.full_import.department_stores.time.monotonic', side_effect=[10, 10, 11, 13]), \
                patch('scrapers.full_import.department_stores.time.sleep') as sleep:
            scraper.get('/product')
            scraper.get('/product')
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0, 2])

    def test_daily_stops_at_first_rejected_style_without_patching_inventory(self):
        for store in ('salling', 'magasin'):
            with self.subTest(store=store):
                database = Mock()
                database.list_rows.return_value = [
                    {'id': i, 'source_parent_id': str(i), 'source_color_id': str(i), 'canonical_url': f'https://example.com/{i}'}
                    for i in range(3)]
                scraper = Mock()
                scraper.scrape.side_effect = StoreRateLimited('HTTP 429')
                args = refresh.parse_args(['--store', store, '--no-delay'])
                with patch.object(daily, 'make_scraper', return_value=scraper):
                    with self.assertRaises(StoreRateLimited) as caught:
                        daily.refresh_store(store, database, args)
                scraper.scrape.assert_called_once()
                scraper.close.assert_called_once()
                database.patch_row.assert_not_called()
                self.assertEqual(caught.exception.stats.skipped, 3)

    def test_import_stops_and_flushes_only_previously_validated_rows(self):
        for store in ('salling', 'magasin'):
            with self.subTest(store=store):
                scraper = Mock()
                scraper.covered_urls = set()
                scraper.clean_url.side_effect = lambda url: url
                scraper.fetch_snapshot.side_effect = [{}, StoreRateLimited('HTTP 429'), {}]
                row = {'source_parent_id': 'one', 'source_color_id': 'blue'}
                scraper.rows_from_snapshot.return_value = [row]
                args = imports.parse_args(store, ['--url', 'https://example.com/1', '--url',
                    'https://example.com/2', '--url', 'https://example.com/3', '--no-delay'])
                database = Mock()
                saved = []
                database.upsert_products.side_effect = lambda table, rows: saved.extend(rows)
                with patch.object(imports, 'load_dotenv'), patch.object(imports, 'env', return_value='fake'), \
                        patch.object(imports, 'SupabaseCatalogClient', return_value=database):
                    result = imports.import_products(store, args, lambda *a, **kw: scraper)
                self.assertEqual(result, 1)
                self.assertEqual(scraper.fetch_snapshot.call_count, 2)
                scraper.rows_from_snapshot.assert_called_once()
                self.assertEqual(saved, [row])
                scraper.close.assert_called_once()

    def test_failed_department_imports_never_reconcile_and_next_store_runs(self):
        with patch('sys.argv', ['sync_store_catalogs.py', '--store', 'salling', '--store', 'magasin']):
            args = sunday.parse_args()
        database, recorder = Mock(), Mock()
        database.list_rows.return_value = []
        with patch.object(sunday, 'load_dotenv'), patch.object(sunday, 'env', return_value='fake'), \
                patch.object(sunday, 'CatalogLifecycleClient', return_value=database), \
                patch.object(sunday, 'CatalogSyncRunRecorder', return_value=recorder), \
                patch.object(sunday, 'run_importer', return_value=1) as run, \
                patch.object(sunday, 'reconcile_catalog') as reconcile:
            result = sunday.sync_catalogs(args)
        self.assertEqual(result, 1)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(recorder.fail.call_count, 2)
        reconcile.assert_not_called()
        database.patch_row.assert_not_called()
