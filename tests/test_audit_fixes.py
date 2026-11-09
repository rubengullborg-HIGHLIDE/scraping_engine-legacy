"""Regression tests for the second round of audit fixes."""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from scrapers.full_import.suitclub import SuitClubScraper
from scrapers.shopify import variant_prices
from scripts import refresh_store_inventory as refresh


def kroner(value):
    return float(value) / 100 if value is not None else None


class VariantPriceTests(unittest.TestCase):
    def test_full_price_size_is_not_struck_through_by_another_sizes_discount(self):
        variants = [
            {"price": 34900, "compare_at_price": None},   # M, full price
            {"price": 39900, "compare_at_price": 59900},  # S, discounted
        ]
        self.assertEqual((349.0, None), variant_prices(variants, kroner))

    def test_discount_on_the_cheapest_size_is_kept(self):
        variants = [{"price": 29900, "compare_at_price": 39900}, {"price": 34900, "compare_at_price": None}]
        self.assertEqual((299.0, 399.0), variant_prices(variants, kroner))

    def test_no_prices(self):
        self.assertEqual((None, None), variant_prices([{"price": None}], kroner))


class SuitClubPriceUnitTests(unittest.TestCase):
    def test_js_feed_integer_ore_and_collection_kroner_strings(self):
        self.assertEqual(999.0, SuitClubScraper._variant_price([{"price": 99900}], "price"))
        self.assertEqual(999.0, SuitClubScraper._variant_price([{"price": "999.00"}], "price"))


class SuitClubRefreshTests(unittest.TestCase):
    def test_missing_storefront_node_fails_the_row_instead_of_writing_nulls(self):
        database, scraper = Mock(), Mock()
        database.list_rows.return_value = [{
            "id": 1, "source_product_id": "123", "canonical_url": "https://suitclub.dk/products/a"}]
        scraper.fetch_product_page.return_value = "<html></html>"
        scraper.fetch_inventory_snapshot.return_value = {"products": {"123": None}, "locations": []}
        args = refresh.parse_args(["--store", "suitclub", "--no-delay"])
        with patch("scrapers.full_import.suitclub.SuitClubScraper", return_value=scraper), \
             patch.object(refresh, "configure_scraper", return_value=scraper), \
             self.assertLogs(refresh.LOG, level="ERROR"):
            stats = refresh.refresh_suitclub(database, args)
        self.assertEqual(1, stats.failed)
        scraper.product_to_row.assert_not_called()
        database.patch_row.assert_not_called()


class RowUrlTests(unittest.TestCase):
    def test_row_without_url_fails_only_that_row(self):
        database, scraper = Mock(), Mock()
        database.list_rows.return_value = [{"id": 1}, {"id": 2, "canonical_url": "https://example.com/2"}]
        builder = Mock(side_effect=ValueError("parse"))
        args = refresh.parse_args(["--store", "cejf", "--no-delay"])
        with self.assertLogs(refresh.LOG, level="ERROR"):
            stats = refresh.refresh_single_row_store(refresh.STORE_SPECS["cejf"], database, args, scraper, builder)
        self.assertEqual(2, stats.failed)
        self.assertEqual(1, builder.call_count)


if __name__ == "__main__":
    unittest.main()
