from __future__ import annotations

import unittest

from scrapers.full_import.cejf import CejfScraper


class CejfScraperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.product = {
            "id": 100,
            "handle": "classic-shirt-navy",
            "title": "Classic Men’s Shirt – Navy",
            "vendor": "Ćejf",
            "product_type": "",
            "body_html": (
                "<p>Made from 100% organic cotton washed oxford, this men’s shirt features "
                "a classic fit. Soft and versatile.</p><p>Made in North Macedonia.</p>"
            ),
            "tags": ["men"],
            "options": [{"name": "Size", "values": ["S", "M"]}],
            "variants": [
                {
                    "id": 1,
                    "title": "S",
                    "option1": "S",
                    "price": "699.00",
                    "compare_at_price": "899.00",
                    "available": True,
                    "sku": None,
                },
                {
                    "id": 2,
                    "title": "M",
                    "option1": "M",
                    "price": "699.00",
                    "compare_at_price": "899.00",
                    "available": False,
                    "sku": None,
                },
            ],
            "images": ["//cdn.shopify.com/shirt.jpg"],
            "_discovery_collections": ["men"],
        }

    def test_row_extracts_store_specific_catalog_fields(self) -> None:
        row = CejfScraper().product_to_row(self.product)

        self.assertEqual("100", row["source_product_id"])
        self.assertEqual("Ćejf", row["brand"])
        self.assertEqual("Navy", row["color"])
        self.assertEqual("Blue", row["color_group"])
        self.assertEqual(699.0, row["current_price"])
        self.assertEqual(899.0, row["list_price"])
        self.assertEqual(["100% organic cotton washed oxford"], row["materials"])
        self.assertEqual("Classic fit", row["fit"])
        self.assertEqual("North Macedonia", row["country_of_origin"])
        self.assertEqual("Shirts", row["category"])
        self.assertEqual(["Clothing", "Shirts"], row["category_path"])

    def test_webshop_boolean_maps_to_single_aarhus_shop_without_inventing_counts(self) -> None:
        row = CejfScraper().product_to_row(self.product)

        self.assertTrue(row["webshop_sizes"][0]["in_stock"])
        self.assertFalse(row["webshop_sizes"][0]["stock_known"])
        self.assertIsNone(row["webshop_sizes"][0]["stock"])
        self.assertTrue(row["aarhus_available"])
        self.assertIsNone(row["aarhus_total_stock"])
        aarhus = row["local_inventory"]["stores"]["cejf-aarhus"]
        self.assertFalse(aarhus["stock_known"])
        self.assertTrue(aarhus["available"])
        self.assertTrue(aarhus["sizes"]["S"]["available"])
        self.assertFalse(aarhus["sizes"]["M"]["available"])

    def test_product_js_integer_prices_are_converted_from_cents(self) -> None:
        product = dict(self.product)
        product["variants"] = [dict(self.product["variants"][0], price=69900, compare_at_price=89900)]

        row = CejfScraper().product_to_row(product)

        self.assertEqual(699.0, row["current_price"])
        self.assertEqual(899.0, row["list_price"])

    def test_material_fit_category_and_color_helpers_cover_current_patterns(self) -> None:
        description = (
            "Our take on relaxed suit trousers, crafted from Portuguese twill fabric. "
            "Made in North Macedonia."
        )

        self.assertEqual("Portuguese twill fabric", CejfScraper._primary_material(description))
        self.assertEqual("Relaxed fit", CejfScraper._fit(description))
        self.assertEqual("Pants", CejfScraper._category("Twill Suit Pant – Grey Melange"))
        self.assertEqual("Grey Melange", CejfScraper._color("Twill Suit Pant – Grey Melange", description))
        self.assertEqual(
            "Indigo",
            CejfScraper._color("Denim Worker Overshirt", "12-oz denim in a deep indigo tone with a navy collar."),
        )


if __name__ == "__main__":
    unittest.main()
