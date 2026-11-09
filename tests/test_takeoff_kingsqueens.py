"""Takeoff CPH and Kings & Queens scrapers, built on scrapers.shopify."""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from scrapers import shopify
from scrapers.full_import.kings_queens import KingsQueensScraper
from scrapers.full_import.takeoff import TakeoffScraper
from scrapers.source_http import SourcePayloadError

FIXTURES = Path(__file__).parent / "fixtures"


def load(path: str):
    return json.loads((FIXTURES / path).read_text())


class SharedShopifyTests(unittest.TestCase):
    def test_money_understands_both_feeds(self):
        self.assertEqual(1550.0, shopify.money(155000))
        self.assertEqual(1550.0, shopify.money("1550.00"))
        self.assertIsNone(shopify.money("0.00"))
        self.assertIsNone(shopify.money(0))

    def test_option_value_from_feed_and_js(self):
        product = {"options": [{"name": "Color", "position": 1}, {"name": "Size", "position": 2}]}
        self.assertEqual("M", shopify.option_value(product, {"option1": "BLACK", "option2": "M"}, ("size",)))
        js = {"options": [{"name": "Farve"}, {"name": "Størrelse"}]}
        self.assertEqual("L", shopify.option_value(js, {"options": ["Sort", "L"]}, ("størrelse",)))

    def test_color_group_shared_vocabulary(self):
        self.assertEqual("Black", shopify.color_group("BLACK"))
        self.assertEqual("Blue", shopify.color_group("Mørk blå"))
        self.assertEqual("Grey", shopify.color_group("Pure Platinum"))
        self.assertIsNone(shopify.color_group("Salute stripe"))

    def test_collection_pagination_guards(self):
        storefront = shopify.ShopifyStorefront("https://example.com", session=Mock())
        page = Mock(); page.json.return_value = {"products": [{"id": n} for n in range(250)]}
        storefront.session.get.return_value = page
        with self.assertRaisesRegex(SourcePayloadError, "repeated"):
            storefront.collection_products("all")
        page.json.return_value = {"products": []}
        with self.assertRaisesRegex(SourcePayloadError, "no products"):
            storefront.collection_products("all")


class TakeoffTests(unittest.TestCase):
    def setUp(self):
        fixture = load("takeoff/product.json")
        self.product, self.inventory = fixture["feed"], fixture["inventory"]
        self.scraper = TakeoffScraper(session=Mock())

    def test_exact_aarhus_and_copenhagen_stock_per_size(self):
        row = self.scraper.product_to_row(self.product, self.inventory)
        aarhus = row["local_inventory"]["stores"]["takeoff-aarhus"]
        self.assertEqual({"available": True, "stock": 1}, aarhus["sizes"]["40.5"])
        self.assertEqual({"available": False, "stock": 0}, aarhus["sizes"]["40"])
        self.assertEqual(1, row["aarhus_total_stock"])
        self.assertTrue(row["aarhus_available"])
        self.assertEqual(4, row["local_total_stock"])  # 1 Aarhus + 3 Copenhagen
        self.assertEqual((1550.0, None), (row["current_price"], row["list_price"]))
        self.assertEqual("Black / White", row["color"])
        self.assertEqual("Black", row["color_group"])
        self.assertEqual(["Footwear", "Sneakers"], row["category_path"])
        self.assertEqual("new", row["condition"])
        self.assertEqual("FZ5808-102", row["source_product_number"])
        # Online "available" is sourcing, not stock: kept only in webshop_sizes.
        self.assertTrue(all(size["in_stock"] for size in row["webshop_sizes"]))

    def test_js_feed_gives_same_prices(self):
        js = copy.deepcopy(self.product)
        for variant in js["variants"]:
            variant["price"] = int(float(variant["price"]) * 100)
            variant["compare_at_price"] = 0
        row = self.scraper.product_to_row(js, self.inventory)
        self.assertEqual(1550.0, row["current_price"])

    def test_incomplete_inventory_raises(self):
        missing_variant = copy.deepcopy(self.inventory)
        missing_variant["body"]["data"]["product"]["variants"]["edges"].pop()
        missing_aarhus = copy.deepcopy(self.inventory)
        missing_aarhus["body"]["data"]["product"]["variants"]["edges"][0]["node"]["storeAvailability"]["edges"].pop(0)
        null_quantity = copy.deepcopy(self.inventory)
        null_quantity["body"]["data"]["product"]["variants"]["edges"][0]["node"]["storeAvailability"]["edges"][0]["node"]["quantityAvailable"] = None
        for payload in (missing_variant, missing_aarhus, null_quantity, {"body": {}}):
            with self.subTest(), self.assertRaises(SourcePayloadError):
                self.scraper.product_to_row(self.product, payload)

    def test_scope_and_preloved(self):
        self.assertTrue(TakeoffScraper.is_catalog_product({"product_type": "Preloved"}))
        self.assertFalse(TakeoffScraper.is_catalog_product({"product_type": "Collectibles"}))
        self.assertFalse(TakeoffScraper.is_catalog_product({"type": "Accessories"}))
        preloved = dict(self.product, product_type="Preloved", tags=["preloved", "preloved-sneakers"])
        row = self.scraper.product_to_row(preloved, self.inventory)
        self.assertEqual("preloved", row["condition"])
        self.assertEqual(["Footwear", "Sneakers"], row["category_path"])


class KingsQueensTests(unittest.TestCase):
    def setUp(self):
        self.product = load("kingsqueens/product.json")
        self.scraper = KingsQueensScraper(session=Mock())
        self.available = (FIXTURES / "kingsqueens/pickup-available.html").read_text()
        self.unavailable = (FIXTURES / "kingsqueens/pickup-unavailable.html").read_text()

    def test_fragment_parsing(self):
        self.assertTrue(KingsQueensScraper.aarhus_available_from_fragment(self.available))
        self.assertFalse(KingsQueensScraper.aarhus_available_from_fragment(self.unavailable))
        without_aarhus = self.available.replace("Århus Bruuns", "Odense")
        for html in ("<div>Page not found</div>", without_aarhus,
                     self.available.replace("is-available\"> <h6 class=\"pickup-availability-list__location body-font\"> K&amp;Q Århus",
                                            "\"> <h6 class=\"pickup-availability-list__location body-font\"> K&amp;Q Århus")):
            with self.subTest(), self.assertRaises(SourcePayloadError):
                KingsQueensScraper.aarhus_available_from_fragment(html)

    def test_row_with_boolean_aarhus_sizes(self):
        ids = [str(variant["id"]) for variant in self.product["variants"]]
        row = self.scraper.product_to_row(self.product, {ids[0]: False, ids[1]: True, ids[2]: False})
        store = row["local_inventory"]["stores"]["kingsqueens-aarhus"]
        self.assertEqual({"available": True, "stock": None}, store["sizes"]["M"])
        self.assertFalse(store["stock_known"])
        self.assertIsNone(row["aarhus_total_stock"])
        self.assertTrue(row["aarhus_available"])
        self.assertEqual("BLACK", row["color"])
        self.assertEqual("Black", row["color_group"])
        self.assertEqual(299.0, row["current_price"])
        self.assertEqual("130_DK0A8CCKBLK1_BLACK", row["source_product_number"])
        self.assertEqual(["Clothing", "T-shirts til mænd"], row["category_path"])

    def test_missing_size_availability_raises(self):
        first = str(self.product["variants"][0]["id"])
        with self.assertRaises(SourcePayloadError):
            self.scraper.product_to_row(self.product, {first: True})

    def test_scope_excludes_accessories_and_gift_cards(self):
        self.assertTrue(KingsQueensScraper.is_catalog_product({"product_type": "Undertøj til mænd"}))
        self.assertTrue(KingsQueensScraper.is_catalog_product({"product_type": ""}))  # untyped suit trousers etc.
        self.assertFalse(KingsQueensScraper.is_catalog_product({"product_type": "Accessories til herre"}))
        self.assertFalse(KingsQueensScraper.is_catalog_product({"product_type": "", "handle": "kings-queens-gift-card"}))

    def test_fetch_availability_one_request_per_size(self):
        self.scraper.storefront.variant_section = Mock(side_effect=[self.available, self.unavailable, self.available])
        result = self.scraper.fetch_aarhus_availability(self.product, variant_delay=0)
        self.assertEqual([True, False, True], list(result.values()))


class SharedImportRunnerTests(unittest.TestCase):
    def test_partial_batch_writes_good_rows_on_source_product_id(self):
        import requests
        from unittest.mock import patch
        from scripts import shopify_import
        from scripts.import_outcomes import EXIT_PARTIAL

        gone = requests.Response(); gone.status_code = 404
        scraper = Mock()
        scraper.discover_products.return_value = [{"handle": "ok", "id": 1}, {"handle": "gone", "id": 2}, {"handle": "bad", "id": 3}]
        scraper.is_catalog_product.return_value = True

        def hydrate(_scraper, product, _args):
            if product["handle"] == "gone":
                raise requests.HTTPError("404", response=gone)
            if product["handle"] == "bad":
                raise SourcePayloadError("broken")
            return {"source_product_id": str(product["id"])}

        store = shopify_import.ShopifyStore("demo", "Demo", "demo_products", lambda: scraper, hydrate, "https://x/products/a")
        args = shopify_import.parse_args(store, ["--no-delay"])
        client = Mock()
        with patch.object(shopify_import, "env", return_value="test"), \
             patch.object(shopify_import, "SupabaseCatalogClient", return_value=client), \
             self.assertLogs("import_demo_products", level="WARNING"):
            self.assertEqual(EXIT_PARTIAL, shopify_import.run_import(store, args))
        client.upsert_products.assert_called_once_with("demo_products", [{"source_product_id": "1"}],
                                                       on_conflict="source_product_id")
        scraper.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
