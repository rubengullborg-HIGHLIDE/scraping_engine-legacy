import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from argparse import Namespace

import requests

from scrapers.system_curl import SystemCurlSession
from scrapers.source_http import StoreRateLimited
from scripts.refresh_store_inventory import configure_scraper


class SystemCurlTests(unittest.TestCase):
    def setUp(self):
        self.client = SystemCurlSession({'User-Agent': 'HIGHLIDE test'}, min_interval=0)
        self.addCleanup(self.client.close)
        self.status = 200
        self.body = b'{"id": 123}'
        self.calls = []

    def curl(self, command, **kwargs):
        self.calls.append((command, kwargs))
        Path(command[command.index('--dump-header') + 1]).write_bytes(
            b'HTTP/2 301\r\nLocation: https://example.com/final\r\n\r\n' +
            f'HTTP/2 {self.status}\r\nContent-Type: application/json; charset=utf-8\r\nRetry-After: 60\r\n\r\n'.encode())
        Path(command[command.index('--output') + 1]).write_bytes(self.body)
        return subprocess.CompletedProcess(command, 0,
            f'{self.status}\nhttps://example.com/final\n2'.encode(), b'')

    def test_json_redirect_and_http_error_preserve_requests_interface(self):
        with patch('scrapers.system_curl.subprocess.run', side_effect=self.curl):
            response = self.client.get('https://example.com/product', params={'locale': 'dk'})
            self.assertEqual(response.json(), {'id': 123})
            self.assertEqual(response.url, 'https://example.com/final')
            self.assertNotIn('Location', response.headers)
            command = self.calls[-1][0]
            self.assertIn('--http2', command)
            self.assertEqual(command[1], '-q')
            self.assertIn('?locale=dk', command[-1])
            self.status = 404
            response = self.client.get('https://example.com/missing')
            with self.assertRaises(requests.HTTPError) as caught:
                response.raise_for_status()
            self.assertEqual(caught.exception.response.status_code, 404)

    def test_stock_post_and_headers_do_not_use_shell_or_expose_token(self):
        with patch('scrapers.system_curl.subprocess.run', side_effect=self.curl):
            self.client.post('https://example.com/inventory', json={'ids': [1, 2]},
                             headers={'X-Shopify-Storefront-Access-Token': 'public-token'})
        command, kwargs = self.calls[0]
        self.assertEqual(json.loads(kwargs['input']), {'ids': [1, 2]})
        self.assertNotIn('public-token', ' '.join(command))
        self.assertNotIn('shell', kwargs)
        self.assertIn('--data-binary', command)

    def test_429_stops_without_retry(self):
        self.status = 429
        with patch('scrapers.system_curl.subprocess.run', side_effect=self.curl):
            with self.assertRaises(StoreRateLimited):
                self.client.get('https://example.com/product')
        self.assertEqual(len(self.calls), 1)

    def test_timeout_does_not_return_partial_body(self):
        with patch('scrapers.system_curl.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 28, b'', b'timed out')):
            with self.assertRaises(requests.Timeout):
                self.client.get('https://example.com/product')

    def test_pacing_is_applied_between_source_requests(self):
        self.client.min_interval = 3
        with patch('scrapers.system_curl.subprocess.run', side_effect=self.curl), \
             patch('scrapers.system_curl.time.monotonic', side_effect=[10, 11, 13]), \
             patch('scrapers.system_curl.time.sleep') as sleep:
            self.client.get('https://example.com/a')
            self.client.get('https://example.com/b')
        sleep.assert_called_once_with(2)

    def test_only_explicit_opt_in_replaces_source_session(self):
        original = requests.Session()
        scraper = Namespace(session=original)
        configure_scraper(scraper, Namespace(source_transport='requests', max_retries=0))
        self.assertIs(scraper.session, original)
        configure_scraper(scraper, Namespace(source_transport='system-curl'))
        self.assertIsInstance(scraper.session, SystemCurlSession)
        scraper.session.close()


if __name__ == '__main__':
    unittest.main()
