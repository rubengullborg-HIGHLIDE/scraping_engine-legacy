from __future__ import annotations

import unittest
from argparse import Namespace
from unittest.mock import Mock, patch

import requests

from scrapers.full_import.kaufmann import KaufmanScraper
from scripts import import_skagen_clothing_products as skagen


class ImportRegressionTests(unittest.TestCase):
    def test_skagen_writes_every_row_including_final_partial_batch(self):
        for count in (1, 50, 51):
            with self.subTest(count=count):
                scraper = Mock()
                scraper.discover_products.return_value = [
                    {"handle": str(index)} for index in range(count)
                ]
                scraper._handle_from_url.side_effect = lambda value: value
                scraper.fetch_product.side_effect = lambda handle: {"id": handle}
                scraper.is_clothing.return_value = True
                scraper.color_handles.return_value = []
                scraper.product_to_row.side_effect = lambda product, html: {"id": product["id"]}
                written = []
                client = Mock()
                client.upsert_products.side_effect = lambda table, rows: written.append(list(rows))
                args = Namespace(table="skagen_clothing_products", dry_run=False,
                    url=None, collection="men", offset=0, limit=None,
                    discover_only=False, no_delay=True, write_batch_size=50)
                with patch.object(skagen, "load_dotenv"), \
                     patch.object(skagen, "env", return_value="test"), \
                     patch.object(skagen, "SupabaseCatalogClient", return_value=client), \
                     patch("scrapers.full_import.skagen_clothing.SkagenClothingScraper", return_value=scraper):
                    self.assertEqual(skagen.import_skagen_clothing_products(args), 0)
                self.assertEqual([row["id"] for batch in written for row in batch],
                                 [str(index) for index in range(count)])
                self.assertTrue(all(0 < len(batch) <= 50 for batch in written))

    def test_kaufmann_sitemap_request_error_is_logged_and_returns_none(self):
        with patch("scrapers.full_import.kaufmann.requests.get",
                   side_effect=requests.RequestException("request failed")), \
             self.assertLogs("scrapers.full_import.kaufmann", level="ERROR"):
            self.assertIsNone(KaufmanScraper(launch_browser=False)._fetch_sitemap_xml("https://example.com/sitemap.xml"))

    def test_kaufmann_corrupt_gzip_is_logged_and_returns_none(self):
        response = Mock(content=b"\x1f\x8bcorrupt gzip")
        with patch("scrapers.full_import.kaufmann.requests.get", return_value=response), \
             self.assertLogs("scrapers.full_import.kaufmann", level="ERROR"):
            self.assertIsNone(KaufmanScraper(launch_browser=False)._fetch_sitemap_xml("https://example.com/sitemap.xml.gz"))
