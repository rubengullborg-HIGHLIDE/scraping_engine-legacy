"""429 handling must stop the source store, not hold up unrelated stores."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

from scripts import refresh_store_inventory as refresh


class RefreshRateLimitTests(unittest.TestCase):
    def args(self):
        return refresh.parse_args(['--store', 'rains', '--store', 'cejf', '--no-delay'])

    def test_429_is_never_retried_even_with_retry_after(self):
        session = requests.Session()
        scraper = SimpleNamespace(session=session)
        refresh.configure_scraper(scraper, self.args())
        retry = session.get_adapter('https://example.com').max_retries
        for header in (False, True):
            self.assertFalse(retry.is_retry('GET', 429, has_retry_after=header))
        self.assertTrue(retry.is_retry('GET', 503))
        self.assertFalse(retry.new().is_retry('GET', 429, has_retry_after=True))
        session.close()

    def test_response_hook_stops_after_one_response_without_sleep(self):
        session = requests.Session()
        refresh.configure_scraper(SimpleNamespace(session=session), self.args())
        response = requests.Response()
        response.status_code = 429
        response.url = 'https://example.com/products/test.js'
        response.headers['Retry-After'] = '86400'
        response._content = b'rate limited'
        response._content_consumed = True
        adapter = session.get_adapter(response.url)
        with patch.object(adapter, 'send', return_value=response) as send, patch('time.sleep') as sleep:
            with self.assertRaisesRegex(refresh.StoreRateLimited, 'Retry-After=86400'):
                session.get(response.url)
            self.assertEqual(send.call_count, 1)
            sleep.assert_not_called()
        session.close()

    def test_generic_store_aborts_before_next_row_or_any_stock_write(self):
        database, scraper = Mock(), SimpleNamespace(session=Mock())
        rows = [{'id': n, 'canonical_url': 'https://example.com/' + str(n)} for n in range(3)]
        database.list_rows.return_value = rows
        builder = Mock(side_effect=refresh.StoreRateLimited('HTTP 429'))
        with self.assertRaises(refresh.StoreRateLimited) as caught:
            refresh.refresh_single_row_store(refresh.STORE_SPECS['romerhus'], database,
                                             self.args(), scraper, builder)
        self.assertEqual(builder.call_count, 1)
        self.assertEqual(caught.exception.stats.skipped, 3)
        self.assertEqual(caught.exception.stats.pages, 1)
        database.patch_row.assert_not_called()
        database.queue_history_observation.assert_not_called()
        scraper.session.close.assert_called_once()

    def test_rains_aborts_first_product_and_retains_existing_stock(self):
        database, scraper = Mock(), Mock()
        database.list_rows.return_value = [
            {'id': n, 'canonical_url': f'https://www.dk.rains.com/products/{n}'} for n in range(3)]
        scraper.fetch_product.side_effect = refresh.StoreRateLimited('HTTP 429')
        with patch('scrapers.full_import.rains.RainsScraper', return_value=scraper), \
                patch.object(refresh, 'configure_scraper', return_value=scraper):
            with self.assertRaises(refresh.StoreRateLimited) as caught:
                refresh.refresh_rains(database, self.args())
        scraper.fetch_product.assert_called_once()
        self.assertEqual(caught.exception.stats.skipped, 3)
        database.patch_row.assert_not_called()
        scraper.session.close.assert_called_once()

    def test_suitclub_config_rate_limit_is_not_swallowed(self):
        database, scraper = Mock(), Mock()
        database.list_rows.return_value = [
            {'id': n, 'canonical_url': f'https://suitclub.dk/products/{n}'} for n in range(3)]
        scraper.fetch_product_page.side_effect = refresh.StoreRateLimited('HTTP 429')
        with patch('scrapers.full_import.suitclub.SuitClubScraper', return_value=scraper), \
                patch.object(refresh, 'configure_scraper', return_value=scraper):
            with self.assertRaises(refresh.StoreRateLimited):
                refresh.refresh_suitclub(database, self.args())
        scraper.fetch_product_page.assert_called_once()
        scraper.fetch_inventory_snapshot.assert_not_called()
        database.patch_row.assert_not_called()

    def test_next_store_runs_and_run_is_finalized_partial_with_progress(self):
        database = Mock()
        blocked = refresh.StoreRateLimited('HTTP 429')
        blocked.stats = refresh.RefreshStats(pages=2, updated=1, history_observations=1, skipped=3)
        first = Mock(side_effect=blocked)
        second = Mock(return_value=refresh.RefreshStats(pages=1, updated=1, history_observations=1))
        args = self.args()
        with patch.object(refresh, 'SupabaseInventoryClient', return_value=database), \
                patch.object(refresh, 'load_dotenv'), patch.object(refresh, 'env', return_value='test'), \
                patch.dict(refresh.STORE_REFRESHERS, rains=first, cejf=second):
            result = refresh.refresh_stores(args)
        self.assertNotEqual(result, 0)
        first.assert_called_once()
        second.assert_called_once()
        payload = database.update_refresh_run.call_args.args[1]
        self.assertEqual(payload['status'], 'partial')
        self.assertEqual(payload['failed_stores'], 1)
        self.assertEqual(payload['updated_products'], 2)
        self.assertEqual(payload['skipped_products'], 3)
        self.assertIsNotNone(payload['finished_at'])
        database.close.assert_called_once()
