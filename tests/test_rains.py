from __future__ import annotations

import unittest

from scrapers.full_import.rains import RainsScraper


class RainsScraperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scraper = RainsScraper()
        self.product = {
            "id": 123,
            "handle": "test-jacket-male",
            "title": "Test Jacket",
            "vendor": "Rains",
            "type": "Long Sleeve",
            "description": "<p>A useful jacket.</p>",
            "tags": ["Male", "layer"],
            "options": [
                {"name": "Color", "position": 1, "values": ["Black", "Bark"]},
                {"name": "Size", "position": 2, "values": ["S"]},
            ],
            "variants": [
                {
                    "id": 1,
                    "option1": "Black",
                    "option2": "S",
                    "title": "Black / S",
                    "sku": "19030\\01\\S",
                    "barcode": "111",
                    "available": True,
                    "price": 99900,
                    "compare_at_price": None,
                    "featured_image": {"position": 2, "src": "//cdn.example/black.jpg"},
                },
                {
                    "id": 2,
                    "option1": "Bark",
                    "option2": "S",
                    "title": "Bark / S",
                    "sku": "19030\\177\\S",
                    "barcode": "222",
                    "available": True,
                    "price": 79900,
                    "compare_at_price": 99900,
                    "featured_image": {"position": 1, "src": "//cdn.example/bark.jpg"},
                },
            ],
            "media": [
                {"media_type": "image", "position": 1, "src": "//cdn.example/bark.jpg"},
                {"media_type": "image", "position": 2, "src": "//cdn.example/black.jpg"},
            ],
            "_collection_handles": ["mens-clothing"],
        }
        self.page_html = """
            <div id="product-tab-details">
              <div class="space-y-4">
                <div><p class="heading">Materiale:</p><p>100% Polyester</p></div>
                <div><p class="heading">Funktioner:</p><p>- Vindtæt<br>- Afslappet pasform</p></div>
              </div>
            </div>
            <div id="product-tab-care-instructions"><ul><li>Maskinvask ved 30°C.</li></ul></div>
            <p>Modellen er 187 cm høj og bruger størrelse M</p>
            <script>
              window.productShopStape.metafields = {};
              window.productShopStape.metafields["custom"] = {};
              window.productShopStape.metafields["custom"]["care_instructions"] = [{"Description":"Machine wash.","Code":"W-C30"}];
              window.productShopStape.metafields["custom"]["full_category_path"] = "Ready to Wear/Everyday RTW/Long Sleeve";
              window.productShopStape.metafields["custom"]["sizeguide"] = [{"Name":"Length","Measurements":[{"Size":"S","Measurement":67.0}]}];
            </script>
        """
        self.inventory = {
            "checked_at": "2026-08-06T12:00:00+00:00",
            "warehouses": {2: {"name": "København"}, 3: {"name": "Aarhus"}, 4: {"name": "Frederiksberg"}},
            "stock_by_warehouse": {
                2: {"19030-177-S": 2},
                3: {"19030-177-S": 1},
                4: {},
            },
        }

    def test_builds_one_row_per_colour_with_exact_store_stock(self) -> None:
        rows = self.scraper.product_to_rows(self.product, self.page_html, self.inventory)
        self.assertEqual(2, len(rows))
        rows_by_color = {row["color"]: row for row in rows}

        bark = rows_by_color["Bark"]
        self.assertEqual("19030", bark["source_parent_id"])
        self.assertEqual("19030-177", bark["source_color_id"])
        self.assertEqual("Brun", bark["color_group"])
        self.assertEqual(799.0, bark["current_price"])
        self.assertEqual(999.0, bark["list_price"])
        self.assertEqual(3, bark["local_total_stock"])
        self.assertEqual(1, bark["aarhus_total_stock"])
        self.assertTrue(bark["aarhus_available"])
        self.assertEqual(2, bark["local_inventory"]["stores"]["rains-copenhagen"]["sizes"]["S"]["stock"])
        self.assertEqual(["100% Polyester"], bark["materials"])
        self.assertEqual("Afslappet pasform", bark["fit"])
        self.assertEqual("Long Sleeve", bark["category"])
        self.assertEqual("W-C30", bark["care_instructions"][0]["code"])
        self.assertEqual("https://cdn.example/bark.jpg", bark["images"][0])

        black = rows_by_color["Black"]
        self.assertEqual(0, black["local_total_stock"])
        self.assertFalse(black["local_available"])
        self.assertIsNone(black["list_price"])

    def test_known_rains_palette_has_no_unknown_groups(self) -> None:
        colours = [
            "Asphalt", "Black", "Blanc", "Comet", "Envy", "Glow", "Lucid",
            "Mystique", "Splinter", "Tempt", "Well", "Bark", "Navy", "Cinder",
        ]
        self.assertNotIn("Anden", {self.scraper._color_group(colour) for colour in colours})

    def test_one_option_product_uses_one_size(self) -> None:
        product = {"options": [{"name": "Color", "position": 1}]}
        variant = {"option1": "Black", "public_title": "Black"}
        self.assertEqual("One Size", self.scraper._variant_size(variant, product))

    def test_excludes_womens_handles_but_keeps_mens_and_unisex_handles(self) -> None:
        self.assertTrue(self.scraper._is_womens_product_handle("tech-woven-bomber-jacket-female"))
        self.assertTrue(self.scraper._is_womens_product_handle("TECH-WOVEN-BOMBER-JACKET-FEMALE/"))
        self.assertTrue(self.scraper._is_womens_product_handle("light-woven-w-skirt"))
        self.assertTrue(self.scraper._is_womens_product_handle("tech-woven-skirt-w"))
        self.assertFalse(self.scraper._is_womens_product_handle("tech-woven-bomber-jacket-male"))
        self.assertFalse(self.scraper._is_womens_product_handle("unisex-puffer-jacket"))


if __name__ == "__main__":
    unittest.main()
