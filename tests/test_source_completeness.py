"""Incomplete source data must raise, and one bad product must not fail a batch."""
from __future__ import annotations

import copy
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from scrapers.full_import.rains import RainsScraper
from scrapers.full_import.romerhus import AARHUS_LOCATION_ID, RomerhusScraper
from scrapers.source_http import SourcePayloadError, StoreRateLimited
from scripts.import_outcomes import EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, ItemOutcomes, is_gone
from tests import test_rains

LOG = logging.getLogger("test")
COPENHAGEN = 111209972098


def http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"HTTP {status}", response=response)


class ItemOutcomeTests(unittest.TestCase):
    def run_items(self, effects):
        outcomes = ItemOutcomes("test", LOG)
        with self.assertLogs(LOG, level="WARNING") if any(effects) else _null():
            for index, effect in enumerate(effects):
                with outcomes.item(str(index)):
                    if effect is not None:
                        raise effect
        return outcomes

    def test_gone_products_are_skipped_without_failing_the_batch(self):
        outcomes = self.run_items([None, http_error(404), http_error(410)])
        self.assertEqual(["1", "2"], outcomes.gone)
        self.assertEqual(EXIT_OK, outcomes.exit_code())

    def test_isolated_failure_is_partial(self):
        outcomes = self.run_items([None, SourcePayloadError("markup changed")])
        self.assertEqual(EXIT_PARTIAL, outcomes.exit_code())

    def test_every_product_failing_is_a_source_failure(self):
        outcomes = self.run_items([http_error(500), SourcePayloadError("x")])
        self.assertEqual(EXIT_FAILED, outcomes.exit_code())

    def test_rate_limit_still_stops_the_batch(self):
        outcomes = ItemOutcomes("test", LOG)
        with self.assertRaises(StoreRateLimited):
            with outcomes.item("a"):
                raise StoreRateLimited("429")

    def test_http_429_stops_the_batch_instead_of_failing_the_product(self):
        outcomes = ItemOutcomes("test", LOG)
        with self.assertRaises(StoreRateLimited):
            with outcomes.item("a"):
                raise http_error(429)
        self.assertEqual([], outcomes.failed)

    def test_server_errors_are_not_gone(self):
        self.assertFalse(is_gone(http_error(500)))
        self.assertTrue(is_gone(http_error(404)))


class ImporterBatchTests(unittest.TestCase):
    def test_stoy_batch_imports_good_products_around_gone_and_broken_ones(self):
        from scripts import import_stoy_products as module

        scraper = Mock()

        def fetch_product(handle):
            if handle == "gone":
                raise http_error(404)
            return {"handle": handle}

        scraper.fetch_product.side_effect = fetch_product
        scraper.product_to_row.side_effect = lambda product, page: (
            (_ for _ in ()).throw(SourcePayloadError("broken")) if product["handle"] == "broken"
            else {"handle": product["handle"]}
        )
        client = Mock()
        args = SimpleNamespace(table="stoy_products", url=["ok", "gone", "broken", "ok2"], offset=0, limit=None,
                               discover_only=False, dry_run=False, no_delay=True, write_batch_size=50,
                               collection=None, catalog_manifest=None)
        with patch("scrapers.full_import.stoy.StoyScraper", return_value=scraper), \
             patch.object(module, "env", return_value="test"), \
             patch.object(module, "SupabaseCatalogClient", return_value=client), \
             self.assertLogs(module.LOG, level="WARNING"):
            self.assertEqual(EXIT_PARTIAL, module.import_stoy_products(args))
        client.upsert_products.assert_called_once_with("stoy_products", [{"handle": "ok"}, {"handle": "ok2"}])


class RomerhusCompletenessTests(unittest.TestCase):
    def setUp(self):
        self.scraper = RomerhusScraper(session=Mock())
        self.stores = self.scraper._tracked_stores({
            AARHUS_LOCATION_ID: {"name": "Aarhus", "localPickupEnabled": True},
            COPENHAGEN: {"name": "Gammeltorv", "localPickupEnabled": True},
        })
        self.product = {"id": 1, "handle": "shirt", "title": "Shirt", "tags": [], "description": "",
                        "price": 29900, "variants": [{"id": 11, "public_title": "M"}, {"id": 12, "public_title": "L"}]}

    def test_missing_copenhagen_entry_means_zero(self):
        stock = {11: {"id": 11, "locations": [{"id": AARHUS_LOCATION_ID, "available": 2}, {"id": COPENHAGEN, "available": 1}]},
                 12: {"id": 12, "locations": [{"id": AARHUS_LOCATION_ID, "available": 0}]}}
        row = self.scraper.product_to_row(self.product, stock, self.stores)
        self.assertEqual(2, row["aarhus_total_stock"])
        self.assertEqual(0, row["local_inventory"]["stores"]["gammeltorv-copenhagen"]["sizes"]["L"]["stock"])

    def test_missing_variant_or_aarhus_entry_raises(self):
        complete = {11: {"id": 11, "locations": [{"id": AARHUS_LOCATION_ID, "available": 2}]},
                    12: {"id": 12, "locations": [{"id": AARHUS_LOCATION_ID, "available": 0}]}}
        missing_variant = {11: complete[11]}
        missing_aarhus = {**complete, 12: {"id": 12, "locations": [{"id": COPENHAGEN, "available": 3}]}}
        for stock in (missing_variant, missing_aarhus, {}):
            with self.subTest(stock=stock), self.assertRaises(SourcePayloadError):
                self.scraper.product_to_row(self.product, stock, self.stores)

    def test_aarhus_pickup_disabled_raises(self):
        with self.assertRaises(SourcePayloadError):
            RomerhusScraper._tracked_stores({
                AARHUS_LOCATION_ID: {"name": "Aarhus", "localPickupEnabled": False},
                COPENHAGEN: {"name": "Gammeltorv", "localPickupEnabled": True},
            })


class RainsCompletenessTests(unittest.TestCase):
    def inventory_response(self, aarhus_items):
        def warehouse(warehouse_id, items):
            return {"warehouseid": warehouse_id, "name": str(warehouse_id), "parsedItems": items}
        item = [{"sku": "19030\\01\\S", "decimaltotal": "1"}]
        response = Mock()
        response.json.return_value = {"locale": "dk", "warehouses": [
            warehouse(2, item), warehouse(3, aarhus_items), warehouse(4, item)]}
        return response

    def test_empty_warehouse_raises_instead_of_zeroing_the_store(self):
        scraper = RainsScraper()
        scraper.session = Mock()
        scraper.session.get.return_value = self.inventory_response([])
        with self.assertRaises(SourcePayloadError):
            scraper.fetch_inventory()
        scraper.session.get.return_value = self.inventory_response([{"sku": "19030\\01\\M", "decimaltotal": "2"}])
        self.assertEqual({"19030-01-M": 2}, scraper.fetch_inventory()["stock_by_warehouse"][3])

    def test_duplicate_size_labels_raise(self):
        fixture = test_rains.RainsScraperTests("test_builds_one_row_per_colour_with_exact_store_stock")
        fixture.setUp()
        product = copy.deepcopy(fixture.product)
        duplicate = dict(product["variants"][1], id=3, sku="19030\\177\\S2")
        product["variants"].append(duplicate)
        with self.assertRaises(SourcePayloadError):
            fixture.scraper.product_to_rows(product, fixture.page_html, fixture.inventory)


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
