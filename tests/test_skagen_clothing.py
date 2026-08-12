from __future__ import annotations

import json
import unittest

from scrapers.full_import.skagen_clothing import SkagenClothingScraper


class SkagenClothingScraperTests(unittest.TestCase):
    def test_inventory_keeps_only_real_shops_and_exact_counts(self) -> None:
        variants = [
            {"id": 1, "title": "XS/28"},
            {"id": 2, "title": "S/30"},
        ]
        payload = [
            {"inventory": {"variant_id": "gid://shopify/ProductVariant/1", "inventoryLevels": [
                {"available": 17, "location": {"name": "Bjørnholms Allé 20 , 1.", "city": "Viby J", "zip": "8260"}},
                {"available": 0, "location": {"name": "Aarhus Butik - Skagen Clothing", "city": "Aarhus C", "zip": "8000"}},
                {"available": 3, "location": {"name": "Skagen Clothing Copenhagen", "city": "København K", "zip": "1157"}},
            ]}},
            {"inventory": {"variant_id": "gid://shopify/ProductVariant/2", "inventoryLevels": [
                {"available": 2, "location": {"name": "Aarhus Butik - Skagen Clothing", "city": "Aarhus C", "zip": "8000"}},
                {"available": 5, "location": {"name": "Skagen Clothing Copenhagen", "city": "København K", "zip": "1157"}},
                {"available": 9, "location": {"name": "Fiktiv location", "city": "Risskov", "zip": "8240"}},
            ]}},
        ]
        html = f"<li data-variant-inventories='{json.dumps(payload)}'></li>"

        inventory, by_variant, raw = SkagenClothingScraper._inventory_from_page(html, variants)

        aarhus = inventory["stores"]["skagen-aarhus"]
        copenhagen = inventory["stores"]["skagen-copenhagen"]
        self.assertEqual(2, aarhus["total_stock"])
        self.assertEqual(8, copenhagen["total_stock"])
        self.assertEqual(3, copenhagen["sizes"]["XS/28"]["stock"])
        self.assertEqual(5, by_variant["2"]["skagen-copenhagen"]["stock"])
        self.assertEqual(2, len(raw))

    def test_row_sums_only_aarhus_and_copenhagen_stock(self) -> None:
        product = {
            "id": 100,
            "handle": "example-black",
            "title": "Example Black",
            "vendor": "Skagen Clothing",
            "type": "T-shirts",
            "price": 34900,
            "compare_at_price": 49900,
            "description": "<p>En Boxy fit T-shirt.</p><ul><li>100% bomuld</li></ul>",
            "variants": [{"id": 1, "title": "M", "sku": "EXAMPLE-BLACK-M", "available": True}],
            "tags": ["ALT TØJ TIL MÆND"],
            "images": [],
        }
        payload = [{"inventory": {"variant_id": "gid://shopify/ProductVariant/1", "inventoryLevels": [
            {"available": 4, "location": {"name": "Aarhus Butik - Skagen Clothing", "city": "Aarhus C", "zip": "8000"}},
            {"available": 5, "location": {"name": "Skagen Clothing Copenhagen", "city": "København K", "zip": "1157"}},
            {"available": 90, "location": {"name": "Bjørnholms Allé 20 , 1.", "city": "Viby J", "zip": "8260"}},
        ]}}]
        html = f"""
          <p>Farve <span>Black</span></p><ul><li><a href="#" aria-label="Black" aria-current="true"></a></li></ul>
          <section><p>Pasform</p><ul class="grid-cols-3"><li></li><li class="bg-black"></li><li></li></ul>
            <ul class="grid-cols-3"><li>Lille</li><li>Normal</li><li>Stor</li></ul></section>
          <p>Modellen er 187cm høj og bruger str L</p>
          <div aria-label="Size Guide"><img src="//cdn.example/size.png"></div>
          <li data-variant-inventories='{json.dumps(payload)}'></li>
        """

        row = SkagenClothingScraper().product_to_row(product, html)

        self.assertEqual(9, row["local_total_stock"])
        self.assertEqual(4, row["aarhus_total_stock"])
        self.assertEqual("Boxy fit", row["fit"])
        self.assertEqual(["100% bomuld"], row["materials"])
        self.assertEqual("Normal", row["specifications"]["fit_indicator"])
        self.assertEqual(187, row["model_info"]["height_cm"])
        self.assertEqual("https://cdn.example/size.png", row["size_guide"]["image_url"])

    def test_color_links_and_non_clothing_filter(self) -> None:
        html = """
          <p>Farve <span>brown</span></p>
          <ul><li><a href="#" aria-label="brown" aria-current="true"></a></li>
          <li><a href="/products/style-grey" aria-label="grey"></a></li></ul>
        """
        self.assertEqual(["style-grey"], SkagenClothingScraper.color_handles(html))
        self.assertFalse(SkagenClothingScraper.is_clothing({"product_type": "Beanie"}))
        self.assertTrue(SkagenClothingScraper.is_clothing({"product_type": "Tank top"}))

    def test_style_reference_removes_size_and_colour(self) -> None:
        variants = [
            {"sku": "SELVEDGE-JEANS-BROWN-XS", "title": "XS/28"},
            {"sku": "SELVEDGE-JEANS-BROWN-S", "title": "S/30"},
            {"sku": "SELVEDGE-JEANS-BROWN-M", "title": "M/32"},
        ]

        self.assertEqual("SELVEDGE-JEANS", SkagenClothingScraper._style_reference(variants, "brown", 1))

    def test_parent_reference_groups_inconsistent_colour_skus_by_swatch_family(self) -> None:
        variants = [
            {"handle": "extra-baggy-denim-jeans-selvedge-brown"},
            {"handle": "extra-baggy-denim-jeans-selvedge-grey"},
            {"handle": "extra-baggy-denim-jeans-selvedge"},
        ]

        parent = SkagenClothingScraper._parent_reference(
            "extra-baggy-denim-jeans-selvedge-brown",
            variants,
            "SELVEDGE-JEANS",
        )

        self.assertEqual("extra-baggy-denim-jeans-selvedge", parent)

    def test_materials_are_compositions_not_marketing_sentences(self) -> None:
        details = SkagenClothingScraper._page_details(
            "",
            {"description": "<p>Vores skjorte er produceret i 55% hør og 45% bomuld, hvilket gør den behagelig.</p>"},
        )

        self.assertEqual(["55% hør og 45% bomuld"], details["materials"])


if __name__ == "__main__":
    unittest.main()
